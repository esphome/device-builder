"""Per-job output sidecar: terminal flush, lazy load, migration, reaping.

Pins that terminal-job output lives on disk (not RAM / not the
metadata blob), active-job output stays inline so a restart still
recovers it, legacy inline output migrates on load, and orphaned
sidecars are reaped.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from esphome_device_builder.controllers.firmware.persistence import (
    _job_log_path,
    _write_job_sidecar,
    read_job_output,
)
from esphome_device_builder.models import FirmwareJob, JobStatus, JobType
from tests.controllers.firmware.conftest import FirmwareControllerFactory


def _blob_jobs(config_dir: Path) -> list[dict]:
    """Return the persisted firmware-job entries from ``.device-builder.json``."""
    raw = json.loads((config_dir / ".device-builder.json").read_text())
    jobs_key = next(k for k in raw if k.endswith("firmware_jobs"))
    return raw[jobs_key]


def _terminal_job(output: list[str]) -> FirmwareJob:
    return FirmwareJob(
        job_id="t1",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.COMPLETED,
        output=output,
        exit_code=0,
    )


@pytest.mark.asyncio
async def test_terminal_output_flushed_to_sidecar_and_stripped_from_blob(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """persist_jobs writes a terminal job's log to disk, clears RAM, and omits it from the blob."""
    job = _terminal_job(["line a\n", "line b\n"])
    controller = firmware_controller_factory(job, with_real_persistence=True, with_queue=True)

    await controller._persist_jobs()

    # RAM cleared, log on disk.
    assert job.output == []
    assert read_job_output("t1") == ["line a\n", "line b\n"]
    # Metadata blob carries no output for the terminal job.
    entries = _blob_jobs(tmp_path)
    assert len(entries) == 1
    assert "output" not in entries[0]


@pytest.mark.asyncio
async def test_active_output_kept_in_ram_and_inline_in_blob(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A running job keeps its output in RAM and inline in the blob (restart recovery)."""
    job = FirmwareJob(
        job_id="r1",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.RUNNING,
        output=["building…\n"],
    )
    controller = firmware_controller_factory(job, with_real_persistence=True, with_queue=True)

    await controller._persist_jobs()

    assert job.output == ["building…\n"]
    assert read_job_output("r1") == []
    entries = _blob_jobs(tmp_path)
    assert entries[0]["output"] == ["building…\n"]


def test_sidecar_round_trip_preserves_terminators() -> None:
    r"""Lines carrying ``\n`` / ``\r`` / no terminator survive write→read unchanged."""
    lines = ["plain\n", "progress\r", "bare-final"]
    _write_job_sidecar("rt1", lines)
    assert read_job_output("rt1") == lines


def test_read_missing_sidecar_returns_empty() -> None:
    """Reading a job with no sidecar yields an empty list, not an error."""
    assert read_job_output("never-written") == []


@pytest.mark.asyncio
async def test_legacy_inline_output_migrates_to_sidecar_on_load(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A pre-existing blob with inline terminal output loads with empty RAM + a sidecar."""
    job = _terminal_job(["legacy a\n", "legacy b\n"])
    blob = {"_firmware_jobs": [job.to_dict()]}
    (tmp_path / ".device-builder.json").write_text(json.dumps(blob))

    controller = firmware_controller_factory(with_real_persistence=True, with_queue=True)
    await controller._load_jobs()

    loaded = controller.state.jobs["t1"]
    assert loaded.output == []
    assert read_job_output("t1") == ["legacy a\n", "legacy b\n"]


@pytest.mark.asyncio
async def test_persist_reaps_orphaned_sidecar(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A sidecar with no matching job is deleted on the next persist."""
    _write_job_sidecar("ghost", ["stale\n"])
    assert _job_log_path("ghost").exists()

    job = _terminal_job(["live\n"])
    controller = firmware_controller_factory(job, with_real_persistence=True, with_queue=True)

    await controller._persist_jobs()

    assert not _job_log_path("ghost").exists()
    assert _job_log_path("t1").exists()
