"""
Per-job output sidecar: terminal flush, lazy load, migration, reaping.

Pins that terminal-job output lives on disk (not RAM / not the
metadata blob), active-job output stays inline so a restart still
recovers it, legacy inline output migrates on load, and orphaned
sidecars are reaped.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from esphome_device_builder.controllers.firmware.persistence import (
    _job_log_path,
    _reconcile_sidecars,
    _write_job_sidecar,
    read_job_output,
)
from esphome_device_builder.models import FirmwareJob, JobStatus, JobType, StreamEvent
from tests.conftest import FakeWebSocketClient
from tests.controllers.firmware.conftest import FirmwareControllerFactory


def _blob_jobs(config_dir: Path) -> list[dict]:
    """Return the persisted firmware-job entries from ``.device-builder.json``."""
    raw = json.loads((config_dir / ".device-builder.json").read_text(encoding="utf-8"))
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


async def test_terminal_output_flushed_to_sidecar_and_stripped_from_blob(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """persist_jobs writes a terminal job's log to disk, clears RAM, and omits it from the blob."""
    job = _terminal_job(["line a\n", "line b\n"])
    controller = firmware_controller_factory(job, with_real_persistence=True, with_queue=True)

    await controller._persist_jobs()

    # RAM cleared, log on disk. The sidecar read resolves
    # ``CORE.data_dir`` (which stats), so run it off the loop as
    # production does via ``run_in_executor``.
    assert job.output == []
    assert await asyncio.to_thread(read_job_output, "t1") == ["line a\n", "line b\n"]
    # Metadata blob carries no output for the terminal job.
    entries = _blob_jobs(tmp_path)
    assert len(entries) == 1
    assert "output" not in entries[0]


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
    assert await asyncio.to_thread(read_job_output, "r1") == []
    entries = _blob_jobs(tmp_path)
    assert entries[0]["output"] == ["building…\n"]


def test_sidecar_round_trip_preserves_terminators() -> None:
    r"""Lines carrying ``\n`` / ``\r`` / no terminator survive write→read unchanged."""
    lines = ["plain\n", "progress\r", "bare-final"]
    _write_job_sidecar("rt1", lines)
    assert read_job_output("rt1") == lines


def test_sidecar_round_trip_does_not_oversplit_on_unicode_separators() -> None:
    r"""Embedded form-feed / Unicode line separators stay within one line.

    The ingest path splits only on ``\n`` / ``\r``, so a line that
    happens to contain a form-feed, vertical-tab, NEL, or line/para
    separator must round-trip as a single line; ``str.splitlines``
    would re-split these and inflate the replayed line count.
    """
    lines = [
        "form\x0cfeed\n",
        "vtab\x0bhere\n",
        "nel\x85char\n",
        "line sep\n",
        "para sep\n",
        "bare-final",
    ]
    _write_job_sidecar("rt2", lines)
    assert read_job_output("rt2") == lines


def test_read_missing_sidecar_returns_empty() -> None:
    """Reading a job with no sidecar yields an empty list, not an error."""
    assert read_job_output("never-written") == []


def test_reconcile_logs_when_dir_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreadable job-log dir is logged, not silently swallowed (orphans would leak)."""

    def _boom(self: Path) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", _boom)
    with caplog.at_level(logging.WARNING):
        _reconcile_sidecars(set())
    assert any("Failed to scan job-log dir" in r.message for r in caplog.records)


def test_read_unreadable_sidecar_logs_and_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A present-but-unreadable sidecar logs a warning instead of silently looking empty."""

    def _boom(self: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", _boom)
    with caplog.at_level(logging.WARNING):
        assert read_job_output("unreadable") == []
    assert any("Failed to read job output sidecar" in r.message for r in caplog.records)


def test_write_sidecar_cleans_up_temp_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed atomic replace unlinks the staged temp file and re-raises."""

    def _boom(self: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", _boom)
    with pytest.raises(OSError, match="replace failed"):
        _write_job_sidecar("fail1", ["x\n"])

    log_dir = _job_log_path("fail1").parent
    assert [p for p in log_dir.iterdir() if p.suffix == ".tmp"] == []


async def test_legacy_inline_output_migrates_to_sidecar_on_load(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A pre-existing blob with inline terminal output loads with empty RAM + a sidecar."""
    job = _terminal_job(["legacy a\n", "legacy b\n"])
    blob = {"_firmware_jobs": [job.to_dict()]}
    (tmp_path / ".device-builder.json").write_text(json.dumps(blob), encoding="utf-8")

    controller = firmware_controller_factory(with_real_persistence=True, with_queue=True)
    await controller._load_jobs()

    loaded = controller.state.jobs["t1"]
    assert loaded.output == []
    assert await asyncio.to_thread(read_job_output, "t1") == ["legacy a\n", "legacy b\n"]


async def test_migration_isolates_per_job_write_failure(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One job's sidecar write failure is logged and skipped; the rest still migrate.

    Write-then-clear with per-job isolation: a failing write leaves
    that job's lines in ``job.output`` (saved by the next persist
    flush) without aborting the batch or blocking startup, and other
    jobs migrate normally.
    """
    bad = FirmwareJob(
        job_id="bad1",
        configuration="a.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.COMPLETED,
        output=["bad a\n"],
        exit_code=0,
    )
    good = FirmwareJob(
        job_id="ok2",
        configuration="b.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.COMPLETED,
        output=["good b\n"],
        exit_code=0,
    )
    blob = {"_firmware_jobs": [bad.to_dict(), good.to_dict()]}
    (tmp_path / ".device-builder.json").write_text(json.dumps(blob), encoding="utf-8")

    def _selective(job_id: str, lines: list[str]) -> None:
        if job_id == "bad1":
            raise OSError("disk full")
        _write_job_sidecar(job_id, lines)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.firmware.persistence._write_job_sidecar", _selective
    )

    controller = firmware_controller_factory(with_real_persistence=True, with_queue=True)
    with caplog.at_level(logging.WARNING):
        await controller._load_jobs()  # does not raise

    # Failed job keeps its output in RAM; the good one migrated to disk.
    assert controller.state.jobs["bad1"].output == ["bad a\n"]
    assert controller.state.jobs["ok2"].output == []
    assert await asyncio.to_thread(read_job_output, "ok2") == ["good b\n"]
    assert any("Failed to migrate job bad1" in r.message for r in caplog.records)


async def test_persist_reaps_orphaned_sidecar(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A sidecar with no matching job is deleted on the next persist."""
    # ``_write_job_sidecar`` / ``_job_log_path().exists()`` resolve
    # ``CORE.data_dir`` (which stats), so drive them off the loop.
    await asyncio.to_thread(_write_job_sidecar, "ghost", ["stale\n"])
    assert await asyncio.to_thread(lambda: _job_log_path("ghost").exists())

    job = _terminal_job(["live\n"])
    controller = firmware_controller_factory(job, with_real_persistence=True, with_queue=True)

    await controller._persist_jobs()

    assert not await asyncio.to_thread(lambda: _job_log_path("ghost").exists())
    assert await asyncio.to_thread(lambda: _job_log_path("t1").exists())


async def test_persist_reaps_orphaned_tmp_file(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A leftover ``.tmp`` staging file (hard kill mid-write) is reaped on the next persist."""

    def _make_orphan_tmp() -> Path:
        log_dir = _job_log_path("x").parent
        log_dir.mkdir(parents=True, exist_ok=True)
        orphan = log_dir / "crashed.abc123.tmp"
        orphan.write_text("partial", encoding="utf-8")
        return orphan

    orphan = await asyncio.to_thread(_make_orphan_tmp)
    assert await asyncio.to_thread(orphan.exists)

    job = _terminal_job(["live\n"])
    controller = firmware_controller_factory(job, with_real_persistence=True, with_queue=True)
    await controller._persist_jobs()

    assert not await asyncio.to_thread(orphan.exists)
    assert await asyncio.to_thread(lambda: _job_log_path("t1").exists())


async def test_concurrent_persist_snapshots_under_lock_and_keeps_all_jobs(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A persist that waited on the lock snapshots the latest jobs, not a stale set.

    Holds the persist lock so the call below parks before it
    snapshots, adds a second job, then releases. With the snapshot
    taken under the lock the persisted blob has both jobs; a
    regression that snapshots before acquiring would drop the
    job added during the wait.
    """
    job_a = _terminal_job(["a\n"])  # job_id "t1"
    controller = firmware_controller_factory(job_a, with_real_persistence=True, with_queue=True)

    await controller._persist_lock.acquire()
    persist_task = asyncio.create_task(controller._persist_jobs())
    # Let the task run up to (and park on) the held lock — and, under a
    # regression, run a pre-lock snapshot before "t2" exists.
    await asyncio.sleep(0)

    job_b = FirmwareJob(
        job_id="t2",
        configuration="b.yaml",
        job_type=JobType.COMPILE,
        status=JobStatus.COMPLETED,
        output=["b\n"],
        exit_code=0,
    )
    controller.state.jobs["t2"] = job_b

    controller._persist_lock.release()
    await persist_task

    assert {e["job_id"] for e in _blob_jobs(tmp_path)} == {"t1", "t2"}


async def test_old_job_log_viewable_via_follow_job_after_restart(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A finished job's log replays over follow_job after a restart loads only metadata."""
    # Controller A finishes a job and persists; its output flushes to
    # the sidecar and drops from RAM.
    job = _terminal_job(["INFO Reading config\n", "INFO Compile finished.\n"])
    writer = firmware_controller_factory(job, with_real_persistence=True, with_queue=True)
    await writer._persist_jobs()
    assert job.output == []

    # Controller B is a fresh process over the same dirs: load restores
    # metadata only, with no output in RAM.
    reader = firmware_controller_factory(
        with_real_persistence=True, with_queue=True, with_real_bus=True
    )
    await reader._load_jobs()
    loaded = reader.state.jobs["t1"]
    assert loaded.status == JobStatus.COMPLETED
    assert loaded.output == []

    # follow_job replays the log from the on-disk sidecar.
    client = FakeWebSocketClient(yield_per_event=True)
    await reader.follow_job(job_id="t1", client=client, message_id="m1")

    assert client.events_for(StreamEvent.OUTPUT) == [
        "INFO Reading config\n",
        "INFO Compile finished.\n",
    ]
    result = client.events_for(StreamEvent.RESULT)
    assert len(result) == 1
    assert result[0]["status"] == "completed"
