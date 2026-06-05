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

from datetime import datetime
from typing import Any

import pytest

from esphome_device_builder.models.firmware import (
    REMOTE_PENDING_JOB_BUILD_SOURCE,
    FirmwareJob,
    JobBuildSource,
    JobSource,
    JobStatus,
    JobType,
)


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


def test_reset_preserves_dispatch_origin_fields() -> None:
    """
    All dispatch-origin fields survive ``reset()``.

    A crash-and-rebuild that lost ``source`` / ``source_label``
    would re-render the rebuild as a LOCAL job; losing
    ``source_pin_sha256`` would strand the runner from its
    receiver (no way back to ``download_artifacts`` /
    ``cancel_job``); losing receiver-side ``remote_peer`` /
    ``remote_job_id`` would strand the receiver's rebuild from
    the offloader's correlation cache. All silent — the build
    still completes, just attributed to the wrong dashboard.
    """
    job = _make_job(
        status=JobStatus.RUNNING,
        source=JobSource.REMOTE,
        source_pin_sha256="a" * 64,
        source_label="desktop",
        source_esphome_version="2026.5.0",
        remote_peer="alpha-dashboard-id",
        remote_job_id="offloader-job-7",
    )

    job.reset()

    assert job.source is JobSource.REMOTE
    assert job.source_pin_sha256 == "a" * 64
    assert job.source_label == "desktop"
    assert job.source_esphome_version == "2026.5.0"
    assert job.remote_peer == "alpha-dashboard-id"
    assert job.remote_job_id == "offloader-job-7"


# ---------------------------------------------------------------------------
# FirmwareJob.mark_running / revert_to_pending_remote
# ---------------------------------------------------------------------------


def test_mark_running_stamps_status_and_start_time() -> None:
    """``mark_running`` sets RUNNING and an ISO 8601 ``started_at``."""
    job = _make_job(status=JobStatus.QUEUED, started_at=None)

    job.mark_running()

    assert job.status is JobStatus.RUNNING
    assert job.started_at is not None
    assert job.started_at.endswith("+00:00")


def test_revert_to_pending_remote_clears_binding_and_run_state() -> None:
    """``revert_to_pending_remote`` resets run state and re-marks REMOTE_PENDING with no pin."""
    job = _make_job(
        status=JobStatus.RUNNING,
        started_at="2026-01-01T00:00:00Z",
        progress=42,
        source=JobSource.REMOTE,
        source_pin_sha256="a" * 64,
        source_label="desktop",
    )

    job.revert_to_pending_remote()

    assert job.status is JobStatus.QUEUED
    assert job.source is JobSource.REMOTE_PENDING
    assert job.source_pin_sha256 == ""
    assert job.started_at is None
    assert job.progress is None


@pytest.mark.parametrize(
    ("status", "terminal", "active"),
    [
        (JobStatus.QUEUED, False, True),
        (JobStatus.RUNNING, False, True),
        (JobStatus.COMPLETED, True, False),
        (JobStatus.FAILED, True, False),
        (JobStatus.CANCELLED, True, False),
    ],
)
def test_is_terminal_is_active_partition_the_statuses(
    status: JobStatus, terminal: bool, active: bool
) -> None:
    """``is_terminal`` / ``is_active`` are exact complements across every status."""
    job = _make_job(status=status)
    assert job.is_terminal is terminal
    assert job.is_active is active


def test_mark_terminal_sets_status_and_completed_at() -> None:
    """``mark_terminal`` stamps a terminal status plus a parseable completion time."""
    job = _make_job(status=JobStatus.RUNNING, completed_at=None)

    job.mark_terminal(JobStatus.COMPLETED)

    assert job.status is JobStatus.COMPLETED
    assert job.completed_at is not None
    assert datetime.fromisoformat(job.completed_at).tzinfo is not None


def test_mark_terminal_records_error_when_given() -> None:
    """``mark_terminal(..., error=...)`` writes the failure message alongside the status."""
    job = _make_job(status=JobStatus.RUNNING, error=None)

    job.mark_terminal(JobStatus.FAILED, error="boom")

    assert job.status is JobStatus.FAILED
    assert job.error == "boom"


def test_mark_terminal_rejects_non_terminal_status() -> None:
    """A non-terminal status raises and leaves the job untouched (no stray completed_at)."""
    job = _make_job(status=JobStatus.RUNNING, completed_at=None)

    with pytest.raises(ValueError, match="non-terminal"):
        job.mark_terminal(JobStatus.RUNNING)

    assert job.completed_at is None
    assert job.status is JobStatus.RUNNING


def test_restore_for_requeue_repools_a_running_remote_compile() -> None:
    """A restored RUNNING remote compile re-queues as REMOTE_PENDING, pin + run cleared."""
    job = _make_job(
        status=JobStatus.RUNNING,
        job_type=JobType.COMPILE,
        source=JobSource.REMOTE,
        source_pin_sha256="a" * 64,
        started_at="2026-01-01T00:00:00Z",
    )

    job.restore_for_requeue()

    assert job.status is JobStatus.QUEUED
    assert job.source is JobSource.REMOTE_PENDING
    assert job.source_pin_sha256 == ""
    assert job.started_at is None  # reset cleared the interrupted run


def test_restore_for_requeue_keeps_a_remote_clean_pinned() -> None:
    """A restored REMOTE CLEAN keeps its server pin — it targets that server on purpose."""
    job = _make_job(
        status=JobStatus.QUEUED,
        job_type=JobType.CLEAN,
        source=JobSource.REMOTE,
        source_pin_sha256="b" * 64,
    )

    job.restore_for_requeue()

    assert job.status is JobStatus.QUEUED
    assert job.source is JobSource.REMOTE
    assert job.source_pin_sha256 == "b" * 64


def test_restore_for_requeue_leaves_a_local_compile_local() -> None:
    """A restored local compile re-queues without being pulled into the remote pool."""
    job = _make_job(status=JobStatus.QUEUED, job_type=JobType.COMPILE, source=JobSource.LOCAL)

    job.restore_for_requeue()

    assert job.status is JobStatus.QUEUED
    assert job.source is JobSource.LOCAL


# ---------------------------------------------------------------------------
# JobBuildSource.for_server / FirmwareJob.apply_build_source
# ---------------------------------------------------------------------------


def test_for_server_bundles_a_remote_source() -> None:
    """``JobBuildSource.for_server`` bundles REMOTE plus the server's pin / label / version."""
    build_source = JobBuildSource.for_server(
        pin_sha256="a" * 64, label="desktop", esphome_version="2026.5.0"
    )

    assert build_source.source is JobSource.REMOTE
    assert build_source.source_pin_sha256 == "a" * 64
    assert build_source.source_label == "desktop"
    assert build_source.source_esphome_version == "2026.5.0"


def test_apply_build_source_stamps_the_job() -> None:
    """``apply_build_source`` copies a bundle's source + pin / label / version onto the job."""
    job = _make_job()

    job.apply_build_source(
        JobBuildSource.for_server(pin_sha256="a" * 64, label="desktop", esphome_version="2026.5.0")
    )

    assert job.source is JobSource.REMOTE
    assert job.source_pin_sha256 == "a" * 64
    assert job.source_label == "desktop"
    assert job.source_esphome_version == "2026.5.0"


def test_apply_build_source_clears_a_prior_binding() -> None:
    """Applying ``REMOTE_PENDING_JOB_BUILD_SOURCE`` drops a prior server binding."""
    job = _make_job(
        source=JobSource.REMOTE,
        source_pin_sha256="a" * 64,
        source_label="desktop",
        source_esphome_version="2026.5.0",
    )

    job.apply_build_source(REMOTE_PENDING_JOB_BUILD_SOURCE)

    assert job.source is JobSource.REMOTE_PENDING
    assert job.source_pin_sha256 == ""
    assert job.source_label == ""
    assert job.source_esphome_version == ""


# ---------------------------------------------------------------------------
# JobSource / source / source_label
# ---------------------------------------------------------------------------


def test_source_defaults_to_local() -> None:
    """
    A freshly-constructed :class:`FirmwareJob` is ``LOCAL`` with no label.

    The default matches every job-row written before this
    field existed — older sidecars deserialise as "this
    dashboard's CPU compiled it", which is what they actually
    represent.
    """
    job = _make_job()
    assert job.source is JobSource.LOCAL
    assert job.source_label == ""


def test_remote_source_round_trips_through_serialisation() -> None:
    """
    A REMOTE-source job survives a mashumaro serialise / deserialise cycle.

    Pins the persistence contract: a dashboard restart can't
    reattribute a REMOTE job to local or lose the receiver
    handle the runner needs to route
    ``download_artifacts`` / ``cancel_job`` against on the
    rebuild.
    """
    job = _make_job(
        source=JobSource.REMOTE,
        source_pin_sha256="a" * 64,
        source_label="desktop",
        source_esphome_version="2026.5.0",
    )

    raw = job.to_json()
    restored = FirmwareJob.from_json(raw)

    assert restored.source is JobSource.REMOTE
    assert restored.source_pin_sha256 == "a" * 64
    assert restored.source_label == "desktop"
    assert restored.source_esphome_version == "2026.5.0"


def test_older_sidecar_without_source_field_loads_as_local() -> None:
    """
    A job-row serialised without the new fields deserialises to LOCAL defaults.

    A field-missing crash on the persistence-load path would
    strand every prior job from the firmware-tasks list on the
    next dashboard start.
    """
    # Hand-crafted minimal job shape with no source /
    # source_pin_sha256 / source_label keys — mirrors what an
    # older firmware-jobs sidecar would have on disk.
    raw = (
        '{"job_id": "old-job-1", '
        '"configuration": "kitchen.yaml", '
        '"job_type": "compile", '
        '"status": "completed", '
        '"created_at": "2025-12-31T23:59:59+00:00"}'
    )

    restored = FirmwareJob.from_json(raw)

    assert restored.job_id == "old-job-1"
    assert restored.source is JobSource.LOCAL
    assert restored.source_pin_sha256 == ""
    assert restored.source_label == ""
    assert restored.source_esphome_version == ""


def test_malformed_source_value_rejected_on_load() -> None:
    """
    A sidecar carrying an unknown ``source`` string fails to deserialise.

    Surfaces a corrupt-write / version-skew at the
    persistence-load boundary rather than letting an unknown
    value ride through into code that branches on
    ``is JobSource.REMOTE``.
    """
    raw = (
        '{"job_id": "j-1", '
        '"configuration": "kitchen.yaml", '
        '"job_type": "compile", '
        '"source": "intergalactic"}'
    )

    with pytest.raises(ValueError):
        FirmwareJob.from_json(raw)
