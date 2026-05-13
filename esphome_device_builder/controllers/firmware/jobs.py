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
    jobs = list(controller._jobs.values())
    if status:
        jobs = [j for j in jobs if j.status == status]
    if configuration:
        jobs = [j for j in jobs if j.configuration == configuration]
    return sorted(jobs, key=attrgetter("created_at"), reverse=True)


async def get_job(controller: FirmwareController, *, job_id: str) -> FirmwareJob | None:
    """Get a specific job with full output."""
    return controller._jobs.get(job_id)


def active_remote_peer_jobs(controller: FirmwareController) -> Iterator[FirmwareJob]:
    """Yield every QUEUED / RUNNING job that arrived via the peer-link.

    Synchronous, no-copy generator over :attr:`_jobs` for the
    peer-link tier's lookups (the 6c cleanup sweep keys off
    this to skip in-flight subtrees; future schedulers /
    diagnostics surfaces should call this rather than
    reaching into ``_jobs`` directly). The single-underscore
    prefix on ``_jobs`` marks it as private to the firmware
    controller; this public accessor is the load-bearing
    seam so a future refactor (lock-wrapped jobs map,
    QUEUED + RUNNING split into two dicts, indexed view)
    doesn't silently break callers.

    ``remote_peer`` filters to peer-link-originated jobs
    only — :class:`FirmwareJob.remote_peer` is empty for
    locally-submitted jobs (see :mod:`models.firmware`).
    """
    for job in controller._jobs.values():
        if job.status not in _ACTIVE_JOB_STATUSES:
            continue
        if not job.remote_peer:
            continue
        yield job


async def cancel(controller: FirmwareController, *, job_id: str) -> None:
    """Cancel a queued or running job.

    Queued jobs are flipped to ``CANCELLED`` immediately. Running
    jobs receive a SIGTERM and are escalated to SIGKILL after a
    short grace period — the runner loop sees the dead process and
    finalises the job with status ``CANCELLED`` (instead of the
    usual ``FAILED`` for non-zero exits) thanks to the
    ``_cancel_requested`` flag set here.

    Either path fires ``JOB_CANCELLED`` on the bus so frontends
    following all-jobs streams stay consistent.

    User-facing rejections (unknown ``job_id``, already-terminal
    job) raise ``CommandError`` so the WS dispatcher surfaces
    the message verbatim. A bare ``ValueError`` would be wrapped
    as ``"Command failed: firmware/cancel"`` and the operator
    would lose the offending id / status. The state-out-of-sync
    case stays as ``RuntimeError`` — it's a server bug, not user
    input, and ``INTERNAL_ERROR`` is the right code.
    """
    job = controller._jobs.get(job_id)
    if not job:
        msg = f"Job not found: {job_id}"
        raise CommandError(ErrorCode.NOT_FOUND, msg)

    if job.status == JobStatus.QUEUED:
        # Mark + persist before fire so a restart-after-cancel
        # reload sees the job as CANCELLED (the test pins
        # this in ``test_cancelled_job_survives_restart_without_
        # being_requeued``). Doesn't go through
        # :meth:`_finalize_terminal` because the helper
        # collapses mark + fire and we need to land
        # ``_persist_jobs`` in between; the slot-release the
        # helper does is a no-op anyway for a QUEUED job
        # (``_current_job`` belongs to whatever's actually
        # running, not this queue entry).
        _mark_job_terminal(job, JobStatus.CANCELLED)
        controller._prune_history()
        await controller._persist_jobs()
        cancelled_payload: JobLifecycleData = {"job": job}
        controller._db.bus.fire(EventType.JOB_CANCELLED, cancelled_payload)
        return

    if job.status == JobStatus.RUNNING:
        if controller._current_job is None or controller._current_job.job_id != job_id:
            msg = "Running job is not the active subprocess (state out of sync)"
            raise RuntimeError(msg)
        controller._cancel_requested.add(job_id)
        # Wake any runner parked on its cancel event (the
        # source-routed remote runner registers one; the
        # local subprocess path doesn't need one because
        # SIGTERM is the wake signal).
        cancel_event = controller._cancel_events.get(job_id)
        if cancel_event is not None:
            cancel_event.set()
        await controller._terminate_current_process()
        return

    msg = f"Cannot cancel a {job.status.value} job"
    raise CommandError(ErrorCode.INVALID_ARGS, msg)


async def clear(controller: FirmwareController, *, status: JobStatus | str | None = None) -> None:
    """
    Remove finished jobs from the list.

    If ``status`` is given, only remove jobs with that status.
    Otherwise removes completed, failed, and cancelled jobs.
    """
    terminal = TERMINAL_JOB_STATUSES
    to_remove = [
        jid
        for jid, job in controller._jobs.items()
        if (status and job.status == status) or (not status and job.status in terminal)
    ]
    for jid in to_remove:
        del controller._jobs[jid]
    await controller._persist_jobs()
