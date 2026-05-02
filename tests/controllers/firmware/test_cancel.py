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
- Already terminal → reject with ``ValueError``.
- Unknown ``job_id`` → reject with ``ValueError``.
- ``RUNNING`` but state out of sync (no ``_current_job`` or wrong
  id) → ``RuntimeError``. Defensive guard against a queue that
  thinks a job is running but the runner has moved on.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.models import EventType, FirmwareJob, JobStatus, JobType


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


def _controller(*jobs: FirmwareJob) -> FirmwareController:
    """Build a controller skeleton with just the bits ``cancel`` reads.

    ``cancel`` consults ``self._jobs`` and ``self._current_job``,
    calls ``self._prune_history`` / ``self._persist_jobs`` /
    ``self._terminate_current_process``, and fires through
    ``self._db.bus``. Everything else (queue, scanner, signal
    handlers) stays out of the test surface.
    """
    controller = FirmwareController.__new__(FirmwareController)
    controller._jobs = {j.job_id: j for j in jobs}
    controller._current_job = None
    controller._current_process = None
    controller._cancel_requested = set()
    controller._persist_jobs = AsyncMock()
    controller._terminate_current_process = AsyncMock()
    bus = MagicMock()
    bus.fire = MagicMock()
    controller._db = type("DB", (), {"bus": bus})()
    return controller


# ---------------------------------------------------------------------------
# Lookup failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_raises_value_error_for_unknown_job_id() -> None:
    """An unknown ``job_id`` raises with the offending id named.

    The WS dispatcher translates ``ValueError`` into a generic
    ``INTERNAL_ERROR`` (it isn't a ``CommandError``), but the
    message is the operator's only clue about what went wrong —
    so pin that the offending id is included verbatim.
    """
    controller = _controller()

    with pytest.raises(ValueError, match="Job not found: bogus"):
        await controller.cancel(job_id="bogus")


# ---------------------------------------------------------------------------
# QUEUED branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_queued_job_marks_terminal_and_fires_event(
    monkeypatch: Any,
) -> None:
    """A queued job flips to ``CANCELLED`` immediately and broadcasts.

    Goes through ``_mark_job_terminal`` (the shared helper that
    stamps ``status`` + ``completed_at``); without it the panel
    would render the cancelled job with a stale ``"queued"``
    status until the next page refresh.
    """
    job = _job("j-q", status=JobStatus.QUEUED)
    controller = _controller(job)
    controller._prune_history = MagicMock()

    await controller.cancel(job_id="j-q")

    assert job.status == JobStatus.CANCELLED
    assert job.completed_at is not None
    controller._db.bus.fire.assert_called_once_with(EventType.JOB_CANCELLED, {"job": job})


@pytest.mark.asyncio
async def test_cancel_queued_job_prunes_history_and_persists() -> None:
    """``cancel`` (QUEUED branch) calls ``_prune_history`` then ``_persist_jobs``.

    Pruning before persist ensures the on-disk metadata reflects
    the post-cancel cap state — without that, a long bulk-cancel
    sequence could spike disk size between calls. ``_persist_jobs``
    is the executor-wrapped writer; pin both so a refactor that
    swaps the order or drops one shows up in the diff.
    """
    job = _job("j-q", status=JobStatus.QUEUED)
    controller = _controller(job)
    controller._prune_history = MagicMock()

    await controller.cancel(job_id="j-q")

    controller._prune_history.assert_called_once()
    controller._persist_jobs.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_queued_does_not_touch_terminate_current_process() -> None:
    """The QUEUED branch never reaches the subprocess terminator.

    Belt-and-braces: ``_terminate_current_process`` walks
    ``self._current_process`` and signals it. For a queued job
    there is no subprocess (and ``_current_process`` is ``None``),
    so calling it here is at best a no-op; at worst a future
    refactor of the terminator that doesn't gracefully handle the
    null case crashes the cancel.
    """
    job = _job("j-q", status=JobStatus.QUEUED)
    controller = _controller(job)
    controller._prune_history = MagicMock()

    await controller.cancel(job_id="j-q")

    controller._terminate_current_process.assert_not_called()


# ---------------------------------------------------------------------------
# RUNNING branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_running_job_records_intent_and_terminates() -> None:
    """A running job's id lands in ``_cancel_requested`` and the terminator runs.

    The runner's ``finally`` branch in ``_execute_job`` consults
    ``_cancel_requested`` to decide whether the dead subprocess
    means CANCELLED (user-initiated) or FAILED (non-zero exit).
    Without the flag, a user-cancelled build would surface as a
    failure in the dashboard log.
    """
    job = _job("j-r", status=JobStatus.RUNNING)
    controller = _controller(job)
    controller._current_job = job

    await controller.cancel(job_id="j-r")

    assert "j-r" in controller._cancel_requested
    controller._terminate_current_process.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_running_job_does_not_fire_event_directly() -> None:
    """``JOB_CANCELLED`` is fired by the *runner*, not by ``cancel``.

    Pin the responsibility split: ``cancel`` records intent and
    nudges the subprocess; the runner's ``finally`` finalises the
    job (with ``_mark_job_terminal``) and fires the event. Firing
    here AND there would double-fire and the all-jobs panel would
    see two cancels for one job.
    """
    job = _job("j-r", status=JobStatus.RUNNING)
    controller = _controller(job)
    controller._current_job = job

    await controller.cancel(job_id="j-r")

    controller._db.bus.fire.assert_not_called()
    # Status stays RUNNING — the runner is what flips it CANCELLED.
    assert job.status == JobStatus.RUNNING


@pytest.mark.asyncio
async def test_cancel_running_job_with_no_current_job_raises_runtime_error() -> None:
    """RUNNING in ``_jobs`` but ``_current_job`` is None → state out of sync.

    Defensive: if we ever see a ``RUNNING`` status with no active
    subprocess, terminating "the current process" would either
    no-op or accidentally signal an unrelated job that started
    after this one's runner exited. Better to surface the
    inconsistency than to mask it.
    """
    job = _job("j-r", status=JobStatus.RUNNING)
    controller = _controller(job)
    # ``_current_job`` left as ``None`` — out of sync.

    with pytest.raises(RuntimeError, match="state out of sync"):
        await controller.cancel(job_id="j-r")
    controller._terminate_current_process.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_running_job_with_mismatched_current_job_raises() -> None:
    """RUNNING ``job_id`` doesn't match ``_current_job.job_id`` → out of sync.

    Same hazard as the no-current-job case: signalling the wrong
    process would terminate a different running job. Refuse the
    cancel rather than send the signal anyway.
    """
    job = _job("j-r", status=JobStatus.RUNNING)
    other = _job("j-other", status=JobStatus.RUNNING)
    controller = _controller(job, other)
    controller._current_job = other  # somebody else is running

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
@pytest.mark.asyncio
async def test_cancel_terminal_job_raises_value_error(status: JobStatus) -> None:
    """A job in any terminal status rejects with ``ValueError``.

    The frontend's "Cancel" button only shows for queued/running
    jobs; this branch catches a stale double-click after the job
    finished. The error message names the actual status so an
    operator looking at the WS log can tell why the cancel was
    refused (race vs. genuinely-finished).
    """
    job = _job("j-t", status=status)
    controller = _controller(job)

    with pytest.raises(ValueError, match=f"Cannot cancel a {status.value} job"):
        await controller.cancel(job_id="j-t")
    controller._terminate_current_process.assert_not_called()
    controller._db.bus.fire.assert_not_called()
