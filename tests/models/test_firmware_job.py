"""Tests for ``FirmwareJob`` model methods.

The ``FirmwareJob`` dataclass is mostly pure data, but a few
methods on it carry meaningful behaviour:

- ``reset()`` — called by the persistence-load path when a
  ``RUNNING`` job survives a dashboard restart and is being
  re-queued for a fresh run. Lives on the model rather than as
  a free helper so a future per-run field added to
  ``FirmwareJob`` lands right next to the method that has to
  clear it.
"""

from __future__ import annotations

from typing import Any

from esphome_device_builder.models.firmware import FirmwareJob, JobStatus, JobType


def _make_job(**overrides: Any) -> FirmwareJob:
    """Minimal ``FirmwareJob`` for these tests."""
    defaults: dict[str, Any] = {
        "job_id": "j-1",
        "configuration": "kitchen.yaml",
        "job_type": JobType.COMPILE,
    }
    defaults.update(overrides)
    return FirmwareJob(**defaults)


# ---------------------------------------------------------------------------
# FirmwareJob.reset
# ---------------------------------------------------------------------------


def test_reset_keeps_log_and_appends_marker() -> None:
    """The pre-crash log survives, with a separator marker appended.

    The build log is useful diagnostic history for "what was
    happening when the dashboard died"; clearing it on recovery
    would lose that. A marker line lets a follower see exactly
    where the rebuild's output starts in the merged buffer.
    """
    job = _make_job(
        status=JobStatus.RUNNING,
        output=["compile in progress\n", "src/main.cpp\n"],
        progress=42,
    )

    job.reset()

    assert job.output[:2] == ["compile in progress\n", "src/main.cpp\n"]
    assert any("dashboard restarted mid-build" in line for line in job.output)
    assert job.output[-1].endswith("\n")


def test_reset_clears_per_run_state() -> None:
    """Per-run state fields reset to their defaults so the rebuild looks fresh.

    Without this, a follower attached to the re-run would see
    the pre-crash ``progress`` / ``exit_code`` / ``started_at``
    leak into the rebuild's status display before the new run
    overwrites them.
    """
    job = _make_job(
        status=JobStatus.RUNNING,
        progress=47,
        error="prior partial error",
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:01:00+00:00",
        exit_code=1,
    )

    job.reset()

    assert job.progress is None
    assert job.error is None
    assert job.started_at is None
    assert job.completed_at is None
    assert job.exit_code is None


def test_reset_does_not_change_status() -> None:
    """The status flip is the caller's responsibility.

    ``reset`` is a state-cleaner; the load path's transition
    (``RUNNING`` → ``QUEUED``) lives at the call site so
    future callers that wanted a different transition don't
    have to fight a hardcoded one.
    """
    job = _make_job(status=JobStatus.RUNNING)
    job.reset()
    assert job.status == JobStatus.RUNNING


def test_reset_preserves_job_identity() -> None:
    """Configuration / job_type / created_at / port / new_name stay intact.

    These describe the job, not the run. A user submitting
    ``rename kitchen → livingroom`` and then crashing should
    have the rebuild target the same rename, not lose the
    new_name and re-run as a vanilla compile.
    """
    job = _make_job(
        status=JobStatus.RUNNING,
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        new_name="livingroom",
        created_at="2025-12-31T23:59:59+00:00",
    )
    job.port = "/dev/ttyUSB0"

    job.reset()

    assert job.configuration == "kitchen.yaml"
    assert job.job_type == JobType.RENAME
    assert job.new_name == "livingroom"
    assert job.port == "/dev/ttyUSB0"
    assert job.created_at == "2025-12-31T23:59:59+00:00"
