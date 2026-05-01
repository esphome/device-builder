"""Tests for the firmware-job → device-state refresh hook.

After a successful compile/install, two things flip:

1. The firmware binary's mtime moves forward — the legacy mtime check
   in ``compute_has_pending_changes`` keys off this. Without a refresh
   the just-flashed device keeps its stale ``has_pending_changes=True``
   (the symptom users see as a still-orange "update pending" dot).
2. The YAML's ``CORE.config_hash`` is now baked into the new firmware,
   so the dashboard persists it as ``expected_config_hash`` so a
   later mDNS resolve can do a hash comparison against the device's
   broadcast (esphome/esphome#16145).

Three pieces are covered:

- ``DeviceScanner.reload`` re-reads a single device's state from disk
  and emits an ``UPDATED`` change, bypassing the cache-key check.
- ``DevicesController._on_firmware_job_completed`` schedules a refresh
  task only for successful COMPILE / UPLOAD / INSTALL jobs.
- ``DevicesController._refresh_after_firmware_job`` writes the freshly
  computed expected hash for COMPILE / INSTALL (UPLOAD reuses the
  prior compile's hash, so it skips the recompute), then reloads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers._device_scanner import (
    DeviceFileMetadata,
    DeviceScanner,
    ScanChange,
)
from esphome_device_builder.helpers.event_bus import Event
from esphome_device_builder.models import (
    Device,
    DeviceState,
    EventType,
    FirmwareJob,
    JobStatus,
    JobType,
)


def _device(name: str = "kitchen", **overrides: Any) -> Device:
    base: dict[str, Any] = {
        "name": name,
        "friendly_name": name.title(),
        "configuration": f"{name}.yaml",
        "address": f"{name}.local",
        "current_version": "2026.5.0",
        "deployed_version": "",
        "state": DeviceState.UNKNOWN,
        "has_pending_changes": True,
    }
    base.update(overrides)
    return Device(**base)


# ----------------------------------------------------------------------
# DeviceScanner.reload
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_rereads_state_and_fires_updated(tmp_path: Path) -> None:
    """Reload re-runs the loader and fires ``UPDATED`` so listeners refresh."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n")

    changes: list[tuple[ScanChange, Device]] = []
    scanner = DeviceScanner(
        config_dir=tmp_path,
        get_metadata=lambda _config_dir, _filename: DeviceFileMetadata(board_id="", ip=""),
        on_change=lambda kind, device: changes.append((kind, device)),
    )

    # Seed the scanner with an initial in-memory snapshot — pre-install
    # state where ``has_pending_changes`` was True.
    initial = _device(has_pending_changes=True)
    scanner._devices[yaml_path] = initial
    scanner._cache_keys[yaml_path] = (0, 0, 0.0, 0)

    refreshed = _device(has_pending_changes=False)
    scanner._load_devices = MagicMock(return_value={yaml_path: refreshed})  # type: ignore[method-assign]

    assert await scanner.reload("kitchen.yaml") is True
    assert scanner._devices[yaml_path] is refreshed
    assert changes == [(ScanChange.UPDATED, refreshed)]


@pytest.mark.asyncio
async def test_reload_unknown_filename_is_noop(tmp_path: Path) -> None:
    """Reload of an untracked file returns False without touching listeners."""
    changes: list[tuple[ScanChange, Device]] = []
    scanner = DeviceScanner(
        config_dir=tmp_path,
        get_metadata=lambda _config_dir, _filename: DeviceFileMetadata(board_id="", ip=""),
        on_change=lambda kind, device: changes.append((kind, device)),
    )

    assert await scanner.reload("ghost.yaml") is False
    assert changes == []


# ----------------------------------------------------------------------
# DevicesController._on_firmware_job_completed
#
# The handler hands the actual work off to ``_refresh_after_firmware_job``
# as a background task. Tests capture which configuration / recompute_hash
# combination was scheduled (or that no task was scheduled at all).
# ----------------------------------------------------------------------


def _make_controller() -> tuple[Any, list[tuple[str, bool]]]:
    """Build a partially-initialised controller and a capture list.

    ``_refresh_after_firmware_job`` is patched with a sync stub that
    records ``(configuration, recompute_hash)`` at call time and
    returns a no-op coroutine. The handler is sync; capturing eagerly
    sidesteps the question of whether the test runs the coroutine.
    """
    from esphome_device_builder.controllers.devices import DevicesController

    captured: list[tuple[str, bool]] = []

    def _capturing_refresh(configuration: str, *, recompute_hash: bool) -> Any:
        captured.append((configuration, recompute_hash))

        async def _noop() -> None:
            return None

        return _noop()

    db = MagicMock()
    db.create_background_task.side_effect = lambda coro: coro.close() or MagicMock()

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._refresh_after_firmware_job = _capturing_refresh  # type: ignore[method-assign]
    return controller, captured


def _job(job_type: JobType, status: JobStatus, configuration: str = "kitchen.yaml") -> FirmwareJob:
    return FirmwareJob(
        job_id="abc123",
        configuration=configuration,
        job_type=job_type,
        status=status,
    )


def test_completed_install_recomputes_hash_and_reloads() -> None:
    """A successful INSTALL recompiles + flashes → hash is fresh, persist it."""
    controller, captured = _make_controller()
    job = _job(JobType.INSTALL, JobStatus.COMPLETED)

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == [("kitchen.yaml", True)]


def test_completed_compile_recomputes_hash_and_reloads() -> None:
    """COMPILE produces a new binary tied to a (potentially) new YAML hash."""
    controller, captured = _make_controller()
    job = _job(JobType.COMPILE, JobStatus.COMPLETED)

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == [("kitchen.yaml", True)]


def test_completed_upload_reloads_without_recomputing_hash() -> None:
    """UPLOAD doesn't recompile — the persisted hash from prior compile still applies."""
    controller, captured = _make_controller()
    job = _job(JobType.UPLOAD, JobStatus.COMPLETED)

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == [("kitchen.yaml", False)]


def test_failed_job_does_not_schedule_refresh() -> None:
    """FAILED jobs leave the device's pending state alone."""
    controller, captured = _make_controller()
    job = _job(JobType.INSTALL, JobStatus.FAILED)

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == []


def test_clean_job_does_not_schedule_refresh() -> None:
    """CLEAN doesn't move the binary mtime in a way that affects has_pending."""
    controller, captured = _make_controller()
    job = _job(JobType.CLEAN, JobStatus.COMPLETED)

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == []


def test_reset_build_env_does_not_schedule_refresh() -> None:
    """RESET_BUILD_ENV has no per-device configuration to refresh."""
    controller, captured = _make_controller()
    job = _job(JobType.RESET_BUILD_ENV, JobStatus.COMPLETED, configuration="")

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == []


def test_event_without_job_payload_is_safe() -> None:
    """Defensive: ``data["job"]`` missing must not raise."""
    controller, captured = _make_controller()

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {}))

    assert captured == []


# ----------------------------------------------------------------------
# DevicesController._refresh_after_firmware_job
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_after_compile_persists_hash_and_reloads(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Successful compile → hash computed + persisted, then device reloaded."""
    from esphome_device_builder.controllers.devices import DevicesController

    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n")

    persisted: list[dict[str, Any]] = []

    def _fake_set_metadata(_config_dir: Path, filename: str, **kwargs: Any) -> None:
        persisted.append({"filename": filename, **kwargs})

    async def _fake_compute(_path: Path) -> str | None:
        return "1a2b3c4d"

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.set_device_metadata",
        _fake_set_metadata,
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.compute_yaml_config_hash",
        _fake_compute,
    )

    db = MagicMock()
    db.settings.config_dir = tmp_path
    db.settings.rel_path = lambda c: tmp_path / c

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.reload = AsyncMock(return_value=True)

    await controller._refresh_after_firmware_job("kitchen.yaml", recompute_hash=True)

    assert persisted == [{"filename": "kitchen.yaml", "expected_config_hash": "1a2b3c4d"}]
    controller._scanner.reload.assert_awaited_once_with("kitchen.yaml")


@pytest.mark.asyncio
async def test_refresh_after_compile_skips_persist_on_hash_failure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """If hash computation fails, fall back to mtime check — don't write empty hash."""
    from esphome_device_builder.controllers.devices import DevicesController

    persisted: list[dict[str, Any]] = []

    def _fake_set_metadata(_config_dir: Path, filename: str, **kwargs: Any) -> None:
        persisted.append({"filename": filename, **kwargs})

    async def _fake_compute(_path: Path) -> str | None:
        return None  # YAML didn't validate, subprocess failed, etc.

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.set_device_metadata",
        _fake_set_metadata,
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.compute_yaml_config_hash",
        _fake_compute,
    )

    db = MagicMock()
    db.settings.config_dir = tmp_path
    db.settings.rel_path = lambda c: tmp_path / c

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.reload = AsyncMock(return_value=True)

    await controller._refresh_after_firmware_job("kitchen.yaml", recompute_hash=True)

    assert persisted == []  # don't overwrite with garbage
    controller._scanner.reload.assert_awaited_once_with("kitchen.yaml")  # reload still happens


@pytest.mark.asyncio
async def test_refresh_after_upload_skips_hash_compute(tmp_path: Path, monkeypatch: Any) -> None:
    """UPLOAD-only doesn't recompile — skip the heavy hash subprocess entirely."""
    from esphome_device_builder.controllers.devices import DevicesController

    compute_calls: list[Path] = []

    async def _fake_compute(path: Path) -> str | None:
        compute_calls.append(path)
        return "deadbeef"

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.compute_yaml_config_hash",
        _fake_compute,
    )

    db = MagicMock()
    db.settings.config_dir = tmp_path
    db.settings.rel_path = lambda c: tmp_path / c

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.reload = AsyncMock(return_value=True)

    await controller._refresh_after_firmware_job("kitchen.yaml", recompute_hash=False)

    assert compute_calls == []
    controller._scanner.reload.assert_awaited_once_with("kitchen.yaml")
