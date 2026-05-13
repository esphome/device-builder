"""Firmware-job lifecycle endpoints: finalize, cancel, terminate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...helpers.process import terminate_subtree_with_grace
from ...models import EventType, FirmwareJob, JobLifecycleData, JobStatus
from .helpers import _mark_job_terminal

if TYPE_CHECKING:
    from .controller import FirmwareController


# Maps each terminal :class:`JobStatus` to the lifecycle event
# the runner fires when a job reaches it. Routes through
# :func:`finalize_terminal` so every finalisation site stays
# paired with the right event.
_STATUS_TO_TERMINAL_EVENT: dict[JobStatus, EventType] = {
    JobStatus.COMPLETED: EventType.JOB_COMPLETED,
    JobStatus.FAILED: EventType.JOB_FAILED,
    JobStatus.CANCELLED: EventType.JOB_CANCELLED,
}


def finalize_terminal(controller: FirmwareController, job: FirmwareJob, status: JobStatus) -> None:
    """
    Stamp *job* terminal, release the runner slot, fire the matching event.

    Step ordering matters: the runner-slot release lands *before*
    the bus.fire so the ``queue_status`` broadcaster's
    synchronous :meth:`queue_status_snapshot` read sees the
    post-terminal idle state. Without that ordering the
    offloader's ``_peer_queue_status`` cache freezes at
    ``running=True`` after the first remote build, and the
    scheduler silently falls back to LOCAL on every subsequent
    install. Callers that want to ride a payload field (e.g.
    ``job.error = "..."``) into the event must set it on the
    job before invoking this helper.
    """
    _mark_job_terminal(job, status)
    if controller._current_job is job:
        controller._current_job = None
        controller._current_process = None
    payload: JobLifecycleData = {"job": job}
    controller._db.bus.fire(_STATUS_TO_TERMINAL_EVENT[status], payload)


def finalize_cancelled(controller: FirmwareController, job: FirmwareJob) -> None:
    """
    Run the runtime-cancel finalisation: discard, mark, fire.

    Doesn't cover the QUEUED-cancel path in
    :meth:`FirmwareController.cancel` itself — that one also
    runs ``_prune_history`` + ``_persist_jobs`` because the
    runner never sees the job, and inlining those here would
    couple the runtime-cancel sites to disk I/O.
    """
    controller._cancel_requested.discard(job.job_id)
    # Route through the bound-method delegate so test patches
    # on ``controller._finalize_terminal`` intercept the cancel
    # path the same way they do every other finalisation site.
    controller._finalize_terminal(job, JobStatus.CANCELLED)


def raise_if_cancelled(controller: FirmwareController, job: FirmwareJob, phase: str) -> None:
    """
    Short-circuit a runner phase if a cancel landed mid-phase.

    Raises ``ValueError`` so :meth:`_execute_job`'s cancel-aware
    ``except Exception`` branch finalises the job as CANCELLED
    (vs. the bare FAILED path used for unrelated exceptions).
    """
    if job.job_id in controller._cancel_requested:
        msg = f"Cancelled during {phase}"
        raise ValueError(msg)


async def terminate_current_process(controller: FirmwareController) -> None:
    """
    Signal the running subprocess (and its children); escalate if it lingers.

    The runner loop is the one that actually finalises the
    :class:`FirmwareJob` on exit — this helper only nudges the
    process. Uses :func:`terminate_subtree_with_grace` so SIGTERM
    walks the whole process group (esphome → platformio → gcc /
    esptool) on POSIX, and ``taskkill /F /T`` on Windows where
    the compile chain doesn't honour polite signals.
    """
    proc = controller._current_process
    if proc is None:
        return
    await terminate_subtree_with_grace(
        proc,
        job_label=f"job {controller._current_job.job_id}" if controller._current_job else "job ?",
    )
