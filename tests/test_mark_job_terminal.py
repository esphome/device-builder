"""Tests for ``firmware._mark_job_terminal``.

The helper consolidates the ``status = X; completed_at = isoformat``
pair that every terminal-job site (queued cancel, mid-run cancel,
normal completion, runner-shutdown cancel, exception path,
reset-build-env cancel/complete) was repeating verbatim. Pin the
contract so a future drive-by edit can't drop one half of the pair
and silently leave a finished job stuck on ``RUNNING`` in the UI.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from esphome_device_builder.controllers.firmware import _mark_job_terminal
from esphome_device_builder.models import FirmwareJob, JobStatus, JobType


def _job() -> FirmwareJob:
    return FirmwareJob(
        job_id="abc123",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
    )


def test_mark_job_terminal_sets_status_and_completed_at() -> None:
    """Both writes happen atomically — neither slot left as the prior value."""
    job = _job()
    assert job.completed_at is None  # sanity

    _mark_job_terminal(job, JobStatus.COMPLETED)

    assert job.status is JobStatus.COMPLETED
    assert job.completed_at is not None
    # ISO 8601 UTC: ``...+00:00``. Don't pin the exact value — just
    # verify it parses as a real timestamp so a future swap to a
    # different formatter doesn't silently produce garbage.
    parsed = datetime.fromisoformat(job.completed_at)
    assert parsed.tzinfo is not None


def test_mark_job_terminal_supports_failed_and_cancelled() -> None:
    """All three terminal states accepted — helper isn't COMPLETED-only."""
    for status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        job = _job()
        _mark_job_terminal(job, status)
        assert job.status is status
        assert job.completed_at is not None


def test_mark_job_terminal_overwrites_existing_completed_at() -> None:
    """Re-marking refreshes the timestamp.

    The reset-build-env path can finalise a job either via the
    cancel branch (status=CANCELLED) or the success branch
    (status=COMPLETED) depending on how the loop exits — and the
    cancel branch can fire mid-iteration. If both paths somehow run
    against the same job (defensive pruning during shutdown), the
    final ``completed_at`` should reflect the actual final
    transition, not whichever one happened first.
    """
    job = _job()
    job.completed_at = "2020-01-01T00:00:00+00:00"

    _mark_job_terminal(job, JobStatus.FAILED)

    assert job.completed_at != "2020-01-01T00:00:00+00:00"
    assert job.status is JobStatus.FAILED


def test_mark_job_terminal_rejects_non_terminal_status() -> None:
    """Calling with QUEUED or RUNNING raises rather than silently stamping.

    Stamping ``completed_at`` on a still-running job mis-orders the
    dashboard's relative-time strings (the UI thinks the job
    finished N seconds ago when it's still actively producing
    output) and confuses the prune-on-shutdown path. Fail loudly on
    the misuse instead.
    """
    for status in (JobStatus.QUEUED, JobStatus.RUNNING):
        job = _job()
        with pytest.raises(ValueError, match="non-terminal"):
            _mark_job_terminal(job, status)
        # Job state is left untouched — neither field gets written.
        assert job.completed_at is None
        assert job.status is JobStatus.RUNNING  # the seed value
