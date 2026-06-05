"""End-to-end coverage for ``FirmwareController.cancel``.

The handler routes by job status:

- ``QUEUED`` → mark terminal + prune + persist + fire
  ``JOB_CANCELLED`` immediately. The runner never sees the job
  (it was waiting in the queue, not running), so finalisation
  happens here rather than in ``_execute_job``'s ``finally``
  branch.
- ``RUNNING`` → record the cancel intent and terminate the
  subprocess. The runner's ``finally`` block sees the dead
  process, finds the id in ``_cancel_requested``, and finalises
  the job with status ``CANCELLED`` instead of the usual
  ``FAILED``-on-non-zero-exit.
- Already terminal → reject with ``CommandError(INVALID_ARGS)``.
- Unknown ``job_id`` → reject with ``CommandError(NOT_FOUND)``.
- ``RUNNING`` but state out of sync (no ``_current_job`` or wrong
  id) → ``RuntimeError``. Defensive guard against a queue that
  thinks a job is running but the runner has moved on. Stays as
  ``RuntimeError`` (server bug, not user input) so the WS
  dispatcher surfaces ``INTERNAL_ERROR``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import (
    ErrorCode,
    EventType,
    FirmwareJob,
    JobStatus,
    JobType,
)
from tests.controllers.firmware.conftest import (
    CaptureEventsFactory,
    FirmwareControllerFactory,
)


def _job(
    job_id: str = "j-1",
    *,
    configuration: str = "kitchen.yaml",
    status: JobStatus = JobStatus.QUEUED,
    job_type: JobType = JobType.COMPILE,
) -> FirmwareJob:
    return FirmwareJob(
        job_id=job_id,
        configuration=configuration,
        job_type=job_type,
        status=status,
    )


# ---------------------------------------------------------------------------
# Lookup failures
# ---------------------------------------------------------------------------


async def test_cancel_raises_not_found_for_unknown_job_id(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """An unknown ``job_id`` raises ``CommandError(NOT_FOUND)`` with the id named.

    Must be a ``CommandError`` (not a bare ``ValueError``) so the
    WS dispatcher surfaces the message verbatim — a ``ValueError``
    would land as the generic ``"Command failed: firmware/cancel"``
    text and the operator would lose the offending id. ``NOT_FOUND``
    is the right semantic code; the frontend's "Cancel" button
    distinguishes it from ``INVALID_ARGS`` to render a different
    error toast.
    """
    controller = firmware_controller_factory(with_settings=False, with_terminate=True)

    with pytest.raises(CommandError) as exc:
        await controller.cancel(job_id="bogus")
    assert exc.value.code == ErrorCode.NOT_FOUND
    assert "Job not found: bogus" in exc.value.message


# ---------------------------------------------------------------------------
# QUEUED branch
# ---------------------------------------------------------------------------


async def test_cancel_queued_job_marks_terminal_and_fires_event(
    firmware_controller_factory: FirmwareControllerFactory,
    capture_firmware_events: CaptureEventsFactory,
) -> None:
    """A queued job flips to ``CANCELLED`` immediately and broadcasts.

    Goes through ``FirmwareJob.mark_terminal`` (which stamps
    ``status`` + ``completed_at``); without it the panel
    would render the cancelled job with a stale ``"queued"``
    status until the next page refresh.
    """
    job = _job("j-q", status=JobStatus.QUEUED)
    controller = firmware_controller_factory(job, with_settings=False, with_terminate=True)
    controller._prune_history = MagicMock()
    captured = capture_firmware_events(controller, EventType.JOB_CANCELLED)

    await controller.cancel(job_id="j-q")

    assert job.status == JobStatus.CANCELLED
    assert job.completed_at is not None
    assert [(e.event_type, e.data) for e in captured] == [(EventType.JOB_CANCELLED, {"job": job})]


async def test_cancel_queued_job_prunes_history_before_persisting(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """``cancel`` (QUEUED branch) calls ``_prune_history`` *before* ``_persist_jobs``.

    Pruning before persist ensures the on-disk metadata reflects
    the post-cancel cap state — without that, a long bulk-cancel
    sequence could spike disk size between calls. ``_persist_jobs``
    is the executor-wrapped writer.

    Pin the order with a flat append-only log shared between the
    two stubs — same idea as ``capture_enqueue_order``. A swap of
    the two calls would change the log's element order and break
    the equality assertion below.
    """
    job = _job("j-q", status=JobStatus.QUEUED)
    controller = firmware_controller_factory(job, with_settings=False, with_terminate=True)
    order: list[str] = []

    def _prune() -> None:
        order.append("prune")

    async def _persist() -> None:
        order.append("persist")

    controller._prune_history = _prune  # type: ignore[method-assign]
    controller._persist_jobs = _persist  # type: ignore[method-assign]

    await controller.cancel(job_id="j-q")

    assert order == ["prune", "persist"]


async def test_cancel_queued_does_not_touch_terminate_current_process(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The QUEUED branch never reaches the subprocess terminator.

    Belt-and-braces: ``_terminate_current_process`` walks
    ``self.state.compile_lane.current_process`` and signals it. For a queued job
    there is no subprocess (and ``_current_process`` is ``None``),
    so calling it here is at best a no-op; at worst a future
    refactor of the terminator that doesn't gracefully handle the
    null case crashes the cancel.
    """
    job = _job("j-q", status=JobStatus.QUEUED)
    controller = firmware_controller_factory(job, with_settings=False, with_terminate=True)
    controller._prune_history = MagicMock()

    await controller.cancel(job_id="j-q")

    controller._terminate_current_process.assert_not_called()


# ---------------------------------------------------------------------------
# RUNNING branch
# ---------------------------------------------------------------------------


async def test_cancel_running_job_records_intent_and_terminates(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A running job's id lands in ``_cancel_requested`` and the terminator runs.

    The runner's ``finally`` branch in ``_execute_job`` consults
    ``_cancel_requested`` to decide whether the dead subprocess
    means CANCELLED (user-initiated) or FAILED (non-zero exit).
    Without the flag, a user-cancelled build would surface as a
    failure in the dashboard log.
    """
    job = _job("j-r", status=JobStatus.RUNNING)
    controller = firmware_controller_factory(job, with_settings=False, with_terminate=True)
    controller.state.compile_lane.current_job = job

    await controller.cancel(job_id="j-r")

    assert "j-r" in controller.state.cancel_requested
    controller._terminate_current_process.assert_awaited_once()


async def test_cancel_running_job_does_not_fire_event_directly(
    firmware_controller_factory: FirmwareControllerFactory,
    capture_firmware_events: CaptureEventsFactory,
) -> None:
    """``JOB_CANCELLED`` is fired by the *runner*, not by ``cancel``.

    Pin the responsibility split: ``cancel`` records intent and
    nudges the subprocess; the runner's ``finally`` finalises the
    job (with ``mark_terminal``) and fires the event. Firing
    here AND there would double-fire and the all-jobs panel would
    see two cancels for one job.
    """
    job = _job("j-r", status=JobStatus.RUNNING)
    controller = firmware_controller_factory(job, with_settings=False, with_terminate=True)
    controller.state.compile_lane.current_job = job
    captured = capture_firmware_events(controller, EventType.JOB_CANCELLED)

    await controller.cancel(job_id="j-r")

    assert captured == []
    # Status stays RUNNING — the runner is what flips it CANCELLED.
    assert job.status == JobStatus.RUNNING


async def test_cancel_running_job_with_no_current_job_raises_runtime_error(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """RUNNING in ``_jobs`` but ``_current_job`` is None → state out of sync.

    Defensive: if we ever see a ``RUNNING`` status with no active
    subprocess, terminating "the current process" would either
    no-op or accidentally signal an unrelated job that started
    after this one's runner exited. Better to surface the
    inconsistency than to mask it.
    """
    job = _job("j-r", status=JobStatus.RUNNING)
    controller = firmware_controller_factory(job, with_settings=False, with_terminate=True)
    # ``_current_job`` left as ``None`` — out of sync.

    with pytest.raises(RuntimeError, match="state out of sync"):
        await controller.cancel(job_id="j-r")
    controller._terminate_current_process.assert_not_called()


async def test_cancel_running_job_with_mismatched_current_job_raises(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """RUNNING ``job_id`` doesn't match ``_current_job.job_id`` → out of sync.

    Same hazard as the no-current-job case: signalling the wrong
    process would terminate a different running job. Refuse the
    cancel rather than send the signal anyway.
    """
    job = _job("j-r", status=JobStatus.RUNNING)
    other = _job("j-other", status=JobStatus.RUNNING)
    controller = firmware_controller_factory(job, other, with_settings=False, with_terminate=True)
    controller.state.compile_lane.current_job = other  # somebody else is running

    with pytest.raises(RuntimeError, match="state out of sync"):
        await controller.cancel(job_id="j-r")
    controller._terminate_current_process.assert_not_called()


# ---------------------------------------------------------------------------
# Already-terminal branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED],
)
async def test_cancel_terminal_job_raises_invalid_args(
    status: JobStatus,
    firmware_controller_factory: FirmwareControllerFactory,
    capture_firmware_events: CaptureEventsFactory,
) -> None:
    """A job in any terminal status rejects with ``CommandError(INVALID_ARGS)``.

    The frontend's "Cancel" button only shows for queued/running
    jobs; this branch catches a stale double-click after the job
    finished. ``CommandError`` (rather than a bare ``ValueError``)
    so the WS dispatcher surfaces the message — the actual status
    is named in it, which is how an operator looking at the WS log
    can tell why the cancel was refused (race vs. genuinely-finished).
    """
    job = _job("j-t", status=status)
    controller = firmware_controller_factory(job, with_settings=False, with_terminate=True)
    captured = capture_firmware_events(controller, EventType.JOB_CANCELLED)

    with pytest.raises(CommandError) as exc:
        await controller.cancel(job_id="j-t")
    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert f"Cannot cancel a {status.value} job" in exc.value.message
    controller._terminate_current_process.assert_not_called()
    assert captured == []
