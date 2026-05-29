"""Job-state queries + lifecycle commands: get_jobs, get_job, cancel, clear."""

from __future__ import annotations

from collections.abc import Iterator
from operator import attrgetter
from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...models import (
    TERMINAL_JOB_STATUSES,
    ErrorCode,
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobStatus,
)
from .constants import _ACTIVE_JOB_STATUSES
from .helpers import _mark_job_terminal

if TYPE_CHECKING:
    from .controller import FirmwareController


async def get_jobs(
    controller: FirmwareController,
    *,
    status: JobStatus | str | None = None,
    configuration: str | None = None,
) -> list[FirmwareJob]:
    """List jobs, optionally filtered by status or configuration."""
    jobs = list(controller.state.jobs.values())
    if status:
        jobs = [j for j in jobs if j.status == status]
    if configuration:
        jobs = [j for j in jobs if j.configuration == configuration]
    return sorted(jobs, key=attrgetter("created_at"), reverse=True)


async def get_job(controller: FirmwareController, *, job_id: str) -> FirmwareJob | None:
    """Get a specific job. Terminal-job ``output`` is empty here; stream it via follow_job."""
    return controller.state.jobs.get(job_id)


def active_remote_peer_jobs(controller: FirmwareController) -> Iterator[FirmwareJob]:
    """Yield every QUEUED / RUNNING job that arrived via the peer-link.

    ``remote_peer`` is empty on locally-submitted jobs so they're
    filtered out; the public accessor exists so callers don't
    reach into ``state.jobs`` directly.
    """
    for job in controller.state.jobs.values():
        if job.status not in _ACTIVE_JOB_STATUSES:
            continue
        if not job.remote_peer:
            continue
        yield job


def find_remote_peer_job(
    controller: FirmwareController, *, remote_peer: str, remote_job_id: str
) -> FirmwareJob | None:
    """Return the FirmwareJob matching (*remote_peer*, *remote_job_id*), or None.

    Linear scan over ``state.jobs.values()``; cardinality is
    bounded by the firmware queue's retention so the scan is
    cheap. Public accessor so cross-package callers (the
    receiver's artifacts-download path) don't reach into
    ``state.jobs`` directly.
    """
    for job in controller.state.jobs.values():
        if job.remote_peer == remote_peer and job.remote_job_id == remote_job_id:
            return job
    return None


def remote_peer_job_ids(controller: FirmwareController, *, remote_peer: str) -> list[str]:
    """Return the ``remote_job_id`` of every job in the queue submitted by *remote_peer*.

    Used by the artifacts-download reject-log to surface the
    available ids when a requested one doesn't match. Public
    accessor so the cross-package caller doesn't reach into
    ``state.jobs`` directly.
    """
    return [j.remote_job_id for j in controller.state.jobs.values() if j.remote_peer == remote_peer]


async def cancel(controller: FirmwareController, *, job_id: str) -> None:
    """Cancel a queued or running job; fires JOB_CANCELLED on the bus.

    QUEUED → flipped to CANCELLED immediately. RUNNING → SIGTERM
    (escalated to SIGKILL after a short grace); the runner sees
    the dead process and finalises the job CANCELLED via
    ``_cancel_requested``.

    User-facing rejections (unknown ``job_id``, already-terminal
    job) raise ``CommandError`` so the WS dispatcher surfaces the
    message verbatim — a bare ``ValueError`` would be wrapped as
    "Command failed: firmware/cancel" and lose the offending id /
    status. State-out-of-sync stays as ``RuntimeError`` (server
    bug, not user input).
    """
    job = controller.state.jobs.get(job_id)
    if not job:
        msg = f"Job not found: {job_id}"
        raise CommandError(ErrorCode.NOT_FOUND, msg)

    if job.status == JobStatus.QUEUED:
        # Mark + persist before fire so a restart-after-cancel reload
        # sees the job as CANCELLED. Spelled out rather than routed
        # through ``_finalize_terminal`` because we need to land
        # ``_persist_jobs`` between the mark and the fire.
        _mark_job_terminal(job, JobStatus.CANCELLED)
        controller._prune_history()
        await controller._persist_jobs()
        cancelled_payload: JobLifecycleData = {"job": job}
        controller._db.bus.fire(EventType.JOB_CANCELLED, cancelled_payload)
        return

    if job.status == JobStatus.RUNNING:
        if controller.state.current_job is None or controller.state.current_job.job_id != job_id:
            msg = "Running job is not the active subprocess (state out of sync)"
            raise RuntimeError(msg)
        controller.state.cancel_requested.add(job_id)
        # Wake any runner parked on its cancel event — only the
        # remote runner registers one; the local subprocess path's
        # wake signal is SIGTERM on the spawned process.
        cancel_event = controller.state.cancel_events.get(job_id)
        if cancel_event is not None:
            cancel_event.set()
        await controller._terminate_current_process()
        return

    msg = f"Cannot cancel a {job.status.value} job"
    raise CommandError(ErrorCode.INVALID_ARGS, msg)


async def clear(controller: FirmwareController, *, status: JobStatus | str | None = None) -> None:
    """Remove finished jobs from the list; pass ``status`` to scope to one state."""
    terminal = TERMINAL_JOB_STATUSES
    to_remove = [
        jid
        for jid, job in controller.state.jobs.items()
        if (status and job.status == status) or (not status and job.status in terminal)
    ]
    for jid in to_remove:
        del controller.state.jobs[jid]
    await controller._persist_jobs()
