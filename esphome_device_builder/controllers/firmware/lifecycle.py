"""Firmware-job lifecycle endpoints: finalize, cancel, terminate."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...helpers.process import terminate_subtree_with_grace
from ...models import EventType, FirmwareJob, JobStatus
from .constants import _PREREQUISITE_FAILED_ERROR
from .helpers import _fire_job_lifecycle, _trim_job_output

if TYPE_CHECKING:
    from ._state import Lane
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)


# Terminal :class:`JobStatus` -> the lifecycle event the runner
# fires when a job reaches it; pinned so every finalisation site
# stays paired with the right event.
_STATUS_TO_TERMINAL_EVENT: dict[JobStatus, EventType] = {
    JobStatus.COMPLETED: EventType.JOB_COMPLETED,
    JobStatus.FAILED: EventType.JOB_FAILED,
    JobStatus.CANCELLED: EventType.JOB_CANCELLED,
}


def finalize_terminal(
    controller: FirmwareController, job: FirmwareJob, status: JobStatus, *, error: str | None = None
) -> None:
    """Stamp *job* terminal (with *error* if given), release the slot, fire the event.

    Step ordering matters: runner-slot release lands *before* the
    ``bus.fire`` so the ``queue_status`` broadcaster's sync
    :meth:`compile_queue_status` read sees the post-terminal
    idle state. Reversing them froze the offloader's
    ``_peer_queue_status`` cache at ``running=True`` after the
    first remote build, silently falling back to LOCAL on every
    subsequent install.
    """
    job.mark_terminal(status, error=error)
    _release_lane_slot(controller, job)
    _fire_job_lifecycle(job, controller._db.bus, _STATUS_TO_TERMINAL_EVENT[status])
    release_dependents(controller, job)
    # Wake an upload lane held behind a now-finished clean/reset (build gate).
    controller.state.build_gate.set()


async def begin_run(controller: FirmwareController, job: FirmwareJob) -> None:
    """Stamp *job* RUNNING, fire ``JOB_STARTED``, and persist — the shared run prologue.

    Both execution paths call this: the lane runner (after claiming its lane
    slot, since the receiver's ``compile_queue_status`` reads ``current_job``
    on ``JOB_STARTED``) and the off-lane dispatch pool. Keeping the prologue
    here means a new field / gate is added once, not mirrored by hand.
    """
    job.mark_running()
    _fire_job_lifecycle(job, controller._db.bus, EventType.JOB_STARTED)
    await controller._persist_jobs()


async def end_run(controller: FirmwareController, job: FirmwareJob) -> None:
    """Terminal bookkeeping (trim + prune) then persist — the shared ``finally`` tail.

    The trim/prune is a no-op while the job is still active. Caller releases its
    own slot (lane ``current_job`` / pool entry) first.
    """
    if job.is_terminal:
        _trim_job_output(job)
        controller._prune_history()
    await controller._persist_jobs()


def finalize_unexpected_error(
    controller: FirmwareController, job: FirmwareJob, exc: BaseException
) -> None:
    """Finalize *job* after an uncaught run exception — cancel intent wins, else FAILED.

    Both execution paths guarantee terminality this way: an exception escaping
    the run must still produce a terminal event, never a job stuck RUNNING.
    """
    if job.job_id in controller.state.cancel_requested:
        finalize_cancelled(controller, job)
        _LOGGER.info("Job %s cancelled before completion: %s", job.job_id, exc)
    else:
        controller._finalize_terminal(job, JobStatus.FAILED, error=str(exc))
        _LOGGER.exception("Job %s failed", job.job_id)


def _release_lane_slot(controller: FirmwareController, job: FirmwareJob) -> None:
    """Clear whichever lane was running *job*."""
    for lane in (controller.state.compile_lane, controller.state.upload_lane):
        if lane.current_job is job:
            lane.current_job = None
            lane.current_process = None
            return


def release_dependents(controller: FirmwareController, job: FirmwareJob) -> bool:
    """Enqueue jobs held on *job* once it succeeds; cancel them if it didn't.

    A chained UPLOAD sits QUEUED but off its lane queue until its prerequisite
    COMPILE finishes (see ``factories.enqueue``); this is where it lands.
    Returns whether any dependent was acted on, so a caller that persisted
    before calling can re-persist when the cascade actually changed state.
    """
    acted = False
    for dep in list(controller.state.jobs.values()):
        if dep.depends_on != job.job_id or dep.status is not JobStatus.QUEUED:
            continue
        acted = True
        if job.status is JobStatus.COMPLETED:
            controller.state.place_on_lane(dep)
        else:
            controller._finalize_terminal(
                dep, JobStatus.CANCELLED, error=_PREREQUISITE_FAILED_ERROR
            )
    return acted


def finalize_cancelled(controller: FirmwareController, job: FirmwareJob) -> None:
    """Runtime-cancel finalisation: discard the cancel flag, finalize as CANCELLED.

    Skips the disk I/O the QUEUED-cancel path in
    :meth:`FirmwareController.cancel` runs (``_prune_history`` +
    ``_persist_jobs``); the runner has already seen the job.
    """
    controller.state.cancel_requested.discard(job.job_id)
    # Route through the bound-method delegate so test patches on
    # ``controller._finalize_terminal`` intercept this path too.
    controller._finalize_terminal(job, JobStatus.CANCELLED)


def cancel_if_requested(controller: FirmwareController, job: FirmwareJob) -> bool:
    """Finalize *job* CANCELLED if a cancel raced in mid-flow; return whether it did.

    The remote runner re-checks at several post-await seams (a cancel can land
    between the receiver's terminal frame and the artifact fetch / local flash);
    each bails its own way, so this returns a flag rather than controlling flow.
    """
    if job.job_id in controller.state.cancel_requested:
        # Delegate so a test patch on ``controller._finalize_cancelled`` still intercepts.
        controller._finalize_cancelled(job)
        return True
    return False


def raise_if_cancelled(controller: FirmwareController, job: FirmwareJob, phase: str) -> None:
    """Raise ``ValueError`` if a cancel landed mid-*phase*; else no-op.

    ``ValueError`` (rather than a custom type) is what the runner's
    cancel-aware ``except Exception`` branch keys off to finalise
    as CANCELLED instead of FAILED.
    """
    if job.job_id in controller.state.cancel_requested:
        msg = f"Cancelled during {phase}"
        raise ValueError(msg)


async def terminate_current_process(controller: FirmwareController, lane: Lane) -> None:
    """Signal *lane*'s running subprocess + children; escalate if it lingers.

    Walks the whole process group via
    :func:`terminate_subtree_with_grace` so SIGTERM reaches
    esphome → platformio → gcc / esptool on POSIX, ``taskkill /F
    /T`` on Windows. The runner loop is what actually finalises
    the job on exit — this helper only nudges the process. Lane-scoped
    so cancelling an upload never signals a concurrent compile.
    """
    proc = lane.current_process
    if proc is None:
        return
    await terminate_subtree_with_grace(
        proc,
        job_label=f"job {lane.current_job.job_id}" if lane.current_job else "job ?",
    )
