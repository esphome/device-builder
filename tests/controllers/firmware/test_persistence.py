"""Coverage for ``_load_jobs`` / ``_persist_jobs``.

The persistence layer round-trips ``self._jobs`` to disk via the
shared ``metadata_transaction`` context (writing under
``_JOBS_KEY`` in ``.device-builder.json``). Existing tests stub
``_persist_jobs`` as an ``AsyncMock`` to verify it was called;
nobody exercised the actual on-disk logic.

Pinned policy (esphome/device-builder#147):

- ``QUEUED`` → re-queue on next boot.
- ``RUNNING`` → ``FAILED`` with a "dashboard restarted" reason.
  The subprocess died with the dashboard, so silently
  re-queueing would re-run a build the user didn't ask for.
- Terminal (``COMPLETED`` / ``FAILED`` / ``CANCELLED``) → load
  into the in-memory map for the recent-jobs panel; don't
  touch ``_queue``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from esphome_device_builder.controllers.firmware.constants import _JOBS_KEY
from esphome_device_builder.models import FirmwareJob, JobStatus, JobType
from tests.controllers.firmware.conftest import FirmwareControllerFactory


def _job(
    job_id: str,
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


def _read_metadata(config_dir: Path) -> dict:
    """Read ``.device-builder.json`` from *config_dir* (or ``{}`` on missing)."""
    path = config_dir / ".device-builder.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# _persist_jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_jobs_writes_empty_list_when_no_jobs(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Empty ``_jobs`` → metadata file gets an empty list under ``_JOBS_KEY``.

    Pin the empty-state shape so a future refactor that omits
    the key entirely (and trips ``_load_jobs``'s ``data.get(...)``
    fallback differently) shows up.
    """
    controller = firmware_controller_factory(with_queue=True)
    controller._persist_jobs = controller.__class__._persist_jobs.__get__(controller)

    await controller._persist_jobs()

    data = _read_metadata(tmp_path)
    assert data == {_JOBS_KEY: []}


@pytest.mark.asyncio
async def test_persist_jobs_round_trips_through_load(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A job written with ``_persist_jobs`` round-trips back through ``_load_jobs``.

    The on-disk shape is the only handoff between dashboard
    instances; pinning the round-trip catches a serializer
    refactor that drops a field or a deserializer that mishandles
    the new shape.
    """
    original = _job(
        "j-1",
        configuration="kitchen.yaml",
        status=JobStatus.COMPLETED,
        job_type=JobType.COMPILE,
    )
    original.exit_code = 0
    original.output = ["compile OK\n"]
    writer = firmware_controller_factory(original, with_queue=True)
    writer._persist_jobs = writer.__class__._persist_jobs.__get__(writer)

    await writer._persist_jobs()

    reader = firmware_controller_factory(with_queue=True)
    await reader._load_jobs()

    assert "j-1" in reader._jobs
    restored = reader._jobs["j-1"]
    assert restored.status == JobStatus.COMPLETED
    assert restored.configuration == "kitchen.yaml"
    assert restored.exit_code == 0
    assert restored.output == ["compile OK\n"]
    # Terminal jobs aren't re-queued.
    reader._queue.put.assert_not_awaited()


# ---------------------------------------------------------------------------
# _load_jobs — status routing
# ---------------------------------------------------------------------------


def _seed_jobs_file(config_dir: Path, *jobs: FirmwareJob) -> None:
    """Write *jobs* into ``.device-builder.json`` under ``_JOBS_KEY``."""
    path = config_dir / ".device-builder.json"
    path.write_text(json.dumps({_JOBS_KEY: [j.to_dict() for j in jobs]}))


@pytest.mark.asyncio
async def test_load_jobs_requeues_queued(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A persisted ``QUEUED`` job is re-put onto the queue on startup.

    Resuming queued-but-not-started jobs is the safety net that
    keeps a dashboard restart during a bulk-install from losing
    the tail of the queue.
    """
    job = _job("j-q", status=JobStatus.QUEUED)
    _seed_jobs_file(tmp_path, job)

    controller = firmware_controller_factory(with_queue=True)

    await controller._load_jobs()

    assert "j-q" in controller._jobs
    assert controller._jobs["j-q"].status == JobStatus.QUEUED
    controller._queue.put.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_jobs_marks_running_as_failed_with_reason(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A persisted ``RUNNING`` job finalises as ``FAILED`` with a reason — NOT re-queued.

    Pinned policy from #147. Silently re-queueing would re-run
    a build the user didn't ask for; surfacing the failure lets
    the user decide to retry. The subprocess died with the
    dashboard, so the build can't be resumed in any meaningful
    sense anyway.
    """
    job = _job("j-r", status=JobStatus.RUNNING)
    _seed_jobs_file(tmp_path, job)

    controller = firmware_controller_factory(with_queue=True)

    await controller._load_jobs()

    restored = controller._jobs["j-r"]
    assert restored.status == JobStatus.FAILED
    assert restored.completed_at is not None  # _mark_job_terminal stamps this
    assert restored.error is not None
    assert "Dashboard restarted" in restored.error
    # Critical: NOT re-queued.
    controller._queue.put.assert_not_awaited()


@pytest.mark.parametrize(
    "status",
    [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED],
)
@pytest.mark.asyncio
async def test_load_jobs_preserves_terminal_history_without_requeueing(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    status: JobStatus,
) -> None:
    """Terminal jobs are loaded into ``_jobs`` but never re-queued.

    The recent-jobs panel renders out of ``_jobs``, and a
    dashboard restart shouldn't blank that history. None of the
    terminal statuses should reach ``_queue.put``.
    """
    job = _job("j-t", status=status)
    _seed_jobs_file(tmp_path, job)

    controller = firmware_controller_factory(with_queue=True)

    await controller._load_jobs()

    assert controller._jobs["j-t"].status == status
    controller._queue.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_jobs_handles_missing_jobs_key(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Metadata file without a ``_JOBS_KEY`` entry → no-op load.

    Cold-start: ``.device-builder.json`` exists for other
    purposes (device metadata, preferences) but the firmware
    queue has never been persisted yet. ``data.get(_JOBS_KEY, [])``
    handles this; pin the contract so a refactor that switches
    to ``data[_JOBS_KEY]`` (KeyError) shows up.
    """
    (tmp_path / ".device-builder.json").write_text(json.dumps({"prefs": {}}))

    controller = firmware_controller_factory(with_queue=True)

    await controller._load_jobs()

    assert controller._jobs == {}
    controller._queue.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_jobs_skips_corrupt_entry_and_continues(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformed entry logs a warning but doesn't kill the rest of the load.

    Defensive: a half-written persistence file (dashboard killed
    mid-write) or an upstream schema change shouldn't make the
    queue unrecoverable on next start. The good entries still
    land; only the corrupt one is dropped.
    """
    import logging

    good = _job("j-good", status=JobStatus.QUEUED)
    payload = {
        _JOBS_KEY: [
            {"job_id": "j-bad", "this_field_does_not_exist_in_FirmwareJob": True},
            good.to_dict(),
        ]
    }
    (tmp_path / ".device-builder.json").write_text(json.dumps(payload))

    controller = firmware_controller_factory(with_queue=True)

    with caplog.at_level(logging.WARNING):
        await controller._load_jobs()

    # Bad entry skipped, warning logged with the offending id.
    assert "j-bad" not in controller._jobs
    assert any("Failed to restore job: j-bad" in rec.message for rec in caplog.records)
    # Good entry survived.
    assert "j-good" in controller._jobs
    controller._queue.put.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_jobs_handles_missing_metadata_file(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A fresh dashboard with no metadata file at all → empty load, no error.

    First-run UX: ``.device-builder.json`` doesn't exist yet.
    ``_load_metadata`` returns ``{}``, the loop doesn't iterate,
    and ``_jobs`` stays empty. Nothing should raise.
    """
    controller = firmware_controller_factory(with_queue=True)

    await controller._load_jobs()

    assert controller._jobs == {}
    controller._queue.put.assert_not_awaited()
