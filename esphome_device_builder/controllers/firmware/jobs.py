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
    JobStatus,
)
from . import lifecycle
from .constants import _ACTIVE_JOB_STATUSES
from .helpers import _fire_job_lifecycle

if TYPE_CHECKING:
    from ._state import Lane
    from .controller import FirmwareController


def _running_lane(controller: FirmwareController, job_id: str) -> Lane | None:
    """Return the lane currently running *job_id*, or None if neither lane is."""
    for lane in (controller.state.compile_lane, controller.state.upload_lane):
        if lane.current_job is not None and lane.current_job.job_id == job_id:
            return lane
    return None


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
        # A pending remote compile is QUEUED but held off-lane in the dispatch
        # pool — remove it so the matcher can't bind it after (and clear an
        # in-flight binding for the non-eager bound-but-not-yet-RUNNING window;
        # the driver's terminal guard then skips the build).
        controller.state.remote_dispatch.discard(job_id)
        # Mark + persist before fire so a restart-after-cancel reload
        # sees the job as CANCELLED. Spelled out rather than routed
        # through ``_finalize_terminal`` because we need to land
        # ``_persist_jobs`` between the mark and the fire.
        job.mark_terminal(JobStatus.CANCELLED)
        controller._prune_history()
        await controller._persist_jobs()
        _fire_job_lifecycle(job, controller._db.bus, EventType.JOB_CANCELLED)
        # Cancel anything held on this job (an install's upload waits on its
        # compile) so a cancelled compile never goes on to flash the device.
        # Persist again when the cascade fired so the dependents' CANCELLED
        # status reaches disk (the persist above was before release_dependents).
        if lifecycle.release_dependents(controller, job):
            controller._prune_history()
            await controller._persist_jobs()
        # Wake an upload lane held behind this job if it was a clean/reset.
        controller.state.build_gate.set()
        return

    if job.status == JobStatus.RUNNING:
        if controller.state.remote_dispatch.is_in_flight(job_id):
            # Off-lane remote compile: no subprocess to SIGTERM —
            # ``run_remote_job`` parks on its cancel event, which sends a wire
            # ``cancel_job``.
            controller.state.request_cancel(job_id)
            return
        lane = _running_lane(controller, job_id)
        if lane is None:
            msg = "Running job is not the active subprocess (state out of sync)"
            raise RuntimeError(msg)
        controller.state.request_cancel(job_id)
        # Lane-scoped so cancelling an upload doesn't signal a concurrent compile.
        await controller._terminate_current_process(lane)
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
