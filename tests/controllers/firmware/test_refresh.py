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

import tempfile as _tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers._device_scanner import (
    DeviceFileMetadata,
    DeviceScanner,
    ScanChange,
)
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.devices._metadata_store import DeviceMetadataStore
from esphome_device_builder.helpers.event_bus import Event
from esphome_device_builder.models import (
    Device,
    EventType,
    FirmwareJob,
    JobStatus,
    JobType,
)
from tests._recording_scanner import RecordingScanner
from tests.conftest import make_device


def _device(name: str = "kitchen", **overrides: Any) -> Device:
    overrides.setdefault("current_version", "2026.5.0")
    return make_device(name=name, **overrides)


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
    scanner._index.set(yaml_path, initial, (0, 0, 0.0, 0))

    refreshed = _device(has_pending_changes=False)
    scanner._load_devices = MagicMock(return_value={yaml_path: refreshed})  # type: ignore[method-assign]

    assert await scanner.reload("kitchen.yaml") is True
    assert scanner.by_path[yaml_path] is refreshed
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


def _make_controller() -> tuple[Any, list[tuple[str, bool, bool]]]:
    """Build a partially-initialised controller and a capture list.

    ``_refresh_after_firmware_job`` is patched with a sync stub that
    records ``(configuration, recompute_hash, flashed)`` at call time
    and returns a no-op coroutine. The handler is sync; capturing
    eagerly sidesteps the question of whether the test runs the
    coroutine.
    """
    captured: list[tuple[str, bool, bool]] = []

    def _capturing_refresh(configuration: str, *, recompute_hash: bool, flashed: bool) -> Any:
        captured.append((configuration, recompute_hash, flashed))

        async def _noop() -> None:
            return None

        return _noop()

    db = MagicMock()
    db.create_background_task.side_effect = lambda coro: coro.close() or MagicMock()

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    # The build-size refresher's ``request`` is the post-CLEAN
    # hand-off; mock it so tests can assert per-job behaviour
    # without needing the full worker lifecycle.
    controller._build_size = MagicMock()
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

    # ``flashed=True`` so the post-reload sync pins
    # ``deployed_config_hash`` and the orange "modified" dot clears
    # without waiting on the rebooted device's mDNS announce.
    assert captured == [("kitchen.yaml", True, True)]


def test_completed_compile_recomputes_hash_and_reloads() -> None:
    """COMPILE produces a new binary tied to a (potentially) new YAML hash."""
    controller, captured = _make_controller()
    job = _job(JobType.COMPILE, JobStatus.COMPLETED)

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    # COMPILE-only didn't push firmware, so ``flashed=False`` — the
    # device on the network still runs the old image and its
    # broadcast hash is still authoritative.
    assert captured == [("kitchen.yaml", True, False)]


def test_completed_upload_reloads_without_recomputing_hash() -> None:
    """UPLOAD doesn't recompile — the persisted hash from prior compile still applies."""
    controller, captured = _make_controller()
    job = _job(JobType.UPLOAD, JobStatus.COMPLETED)

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    # UPLOAD pushes the previously-compiled binary, so the device's
    # firmware is now what ``expected_config_hash`` describes —
    # ``flashed=True``.
    assert captured == [("kitchen.yaml", False, True)]


def test_failed_job_does_not_schedule_refresh() -> None:
    """FAILED jobs leave the device's pending state alone."""
    controller, captured = _make_controller()
    job = _job(JobType.INSTALL, JobStatus.FAILED)

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == []


def test_clean_job_skips_full_refresh_but_pokes_build_size() -> None:
    """CLEAN skips the hash / flash bookkeeping path but pokes the build-size cache.

    The build tree has just been wiped, so the cached
    ``build_size_bytes`` triple is now stale (pre-clean
    non-zero, current dir mtime → 0). The job-completion hook
    pokes the build-size worker for this device; the worker's
    pair-equality short-circuit then walks once to clear the
    cache. ``_refresh_after_firmware_job`` (hash recompute,
    optimistic flash sync) doesn't apply to CLEAN.
    """
    controller, captured = _make_controller()
    job = _job(JobType.CLEAN, JobStatus.COMPLETED)

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == []
    controller._build_size.request.assert_called_once_with("kitchen.yaml")


def test_reset_build_env_does_not_schedule_refresh() -> None:
    """RESET_BUILD_ENV has no per-device configuration to refresh."""
    controller, captured = _make_controller()
    job = _job(JobType.RESET_BUILD_ENV, JobStatus.COMPLETED, configuration="")

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == []


def test_receiver_side_remote_build_job_skips_refresh() -> None:
    """Remote-build configurations skip the refresh and build-size hooks."""
    controller, captured = _make_controller()
    job = _job(
        JobType.INSTALL,
        JobStatus.COMPLETED,
        configuration=".esphome/.remote_builds/abc/kitchen/kitchen.yaml",
    )

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == []
    controller._build_size.request.assert_not_called()


def test_unhandled_job_type_with_configuration_falls_through_silently() -> None:
    """Job types outside CLEAN/COMPILE/UPLOAD/INSTALL/RENAME bail at the type check.

    Belt-and-braces test for the post-CLEAN dispatch table — a
    ``RESET_BUILD_ENV`` job that did happen to carry a
    configuration (or any future job type we haven't wired
    explicitly) bails at the ``if job_type not in (...)`` guard
    *after* the empty-configuration short-circuit, leaving the
    refresh + build-size hooks alone.
    """
    controller, captured = _make_controller()
    job = _job(
        JobType.RESET_BUILD_ENV,
        JobStatus.COMPLETED,
        configuration="kitchen.yaml",
    )

    controller._on_firmware_job_completed(Event(EventType.JOB_COMPLETED, {"job": job}))

    assert captured == []
    controller._build_size.request.assert_not_called()


# ----------------------------------------------------------------------
# DevicesController._refresh_after_firmware_job
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_after_compile_persists_hash_and_reloads(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Successful compile → hash computed + persisted to the store, device reloaded."""
    yaml_path = tmp_path / "kitchen.yaml"
    yaml_path.write_text("esphome:\n  name: kitchen\n")

    async def _fake_compute(_path: Path) -> str | None:
        return "1a2b3c4d"

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.firmware_sync.compute_yaml_config_hash",
        _fake_compute,
    )

    db = MagicMock()
    db.settings.config_dir = tmp_path
    db.settings.rel_path = lambda c: tmp_path / c

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = RecordingScanner()
    controller._build_size = MagicMock()
    controller._shutdown_callbacks = []
    controller._metadata_store = DeviceMetadataStore(
        config_dir=tmp_path,
        data_dir=tmp_path,
        shutdown_register=controller._shutdown_callbacks.append,
    )

    await controller._refresh_after_firmware_job("kitchen.yaml", recompute_hash=True, flashed=False)

    assert controller._metadata_store.get("kitchen.yaml") == {"expected_config_hash": "1a2b3c4d"}
    assert controller._scanner.calls == [("reload", "kitchen.yaml")]


@pytest.mark.asyncio
async def test_refresh_after_compile_skips_persist_on_hash_failure(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """If hash computation fails, fall back to mtime check — don't write empty hash."""

    async def _fake_compute(_path: Path) -> str | None:
        return None  # YAML didn't validate, subprocess failed, etc.

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.firmware_sync.compute_yaml_config_hash",
        _fake_compute,
    )

    db = MagicMock()
    db.settings.config_dir = tmp_path
    db.settings.rel_path = lambda c: tmp_path / c

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = RecordingScanner()
    controller._build_size = MagicMock()
    controller._shutdown_callbacks = []
    tmp_dir_obj = _tempfile.TemporaryDirectory(prefix="dmstore_")
    tmp_dir = Path(tmp_dir_obj.name)
    controller._tmpdir = tmp_dir_obj  # keep alive
    controller._metadata_store = DeviceMetadataStore(
        config_dir=tmp_dir,
        data_dir=tmp_dir,
        shutdown_register=controller._shutdown_callbacks.append,
    )

    await controller._refresh_after_firmware_job("kitchen.yaml", recompute_hash=True, flashed=False)

    # Hash compute failed → no write to the store.
    assert controller._metadata_store.snapshot_all() == {}
    assert controller._scanner.calls == [("reload", "kitchen.yaml")]


@pytest.mark.asyncio
async def test_refresh_after_upload_skips_hash_compute(tmp_path: Path, monkeypatch: Any) -> None:
    """UPLOAD-only doesn't recompile — skip the heavy hash subprocess entirely."""
    compute_calls: list[Path] = []

    async def _fake_compute(path: Path) -> str | None:
        compute_calls.append(path)
        return "deadbeef"

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.firmware_sync.compute_yaml_config_hash",
        _fake_compute,
    )

    db = MagicMock()
    db.settings.config_dir = tmp_path
    db.settings.rel_path = lambda c: tmp_path / c

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = RecordingScanner()
    controller._build_size = MagicMock()

    await controller._refresh_after_firmware_job("kitchen.yaml", recompute_hash=False, flashed=True)

    assert compute_calls == []
    assert controller._scanner.calls == [("reload", "kitchen.yaml")]


# ----------------------------------------------------------------------
# DevicesController._sync_deployed_hash_after_flash
#
# Drives the orange-dot-clears-after-OTA fix. The reloaded device
# inherits ``deployed_config_hash`` from the previous in-memory
# snapshot — typically the now-stale pre-flash mDNS value — so without
# this sync ``has_pending_changes`` reads ``expected != deployed`` and
# the user sees a still-orange dot until the rebooted device's mDNS
# announce propagates (seconds at best, "never" if the rebroadcast
# gets dropped on the wire).
# ----------------------------------------------------------------------


def _flush_controller(device: Device) -> tuple[Any, list[Any]]:
    """Build a controller seeded with *device* and a fired-events list."""
    fired: list[Any] = []

    db = MagicMock()
    db.bus.fire.side_effect = lambda event_type, payload: fired.append((event_type, payload))

    scanner = MagicMock()
    scanner.devices = [device]
    scanner.get_by_name = lambda name: [device] if device.name == name else []

    state_monitor = MagicMock()
    # Drive the same de-dup behaviour real ``apply_config_hash`` has —
    # if the scan device's name matches and the hash differs from the
    # cached value, fire the controller's ``_on_config_hash_change``
    # callback so the assertion can verify the device fields flipped.
    cache: dict[str, str] = {}

    def _apply(name: str, config_hash: str) -> bool:
        if not config_hash:
            return False
        if cache.get(name) == config_hash:
            return False
        cache[name] = config_hash
        # Mirror what ``DeviceStateMonitor`` does — fire the callback
        # the controller registered when wiring up the monitor.
        controller._on_config_hash_change(name, config_hash)
        return True

    state_monitor.apply_config_hash.side_effect = _apply

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = scanner
    controller._state_monitor = state_monitor
    controller._shutdown_callbacks = []
    tmp_dir_obj = _tempfile.TemporaryDirectory(prefix="dmstore_")
    tmp_dir = Path(tmp_dir_obj.name)
    controller._tmpdir = tmp_dir_obj  # keep alive
    controller._metadata_store = DeviceMetadataStore(
        config_dir=tmp_dir,
        data_dir=tmp_dir,
        shutdown_register=controller._shutdown_callbacks.append,
    )
    return controller, fired


@pytest.mark.asyncio
async def test_sync_after_flash_pins_deployed_hash_and_clears_pending() -> None:
    """Post-flash sync flips deployed → expected and emits ``DEVICE_UPDATED``."""
    device = _device(
        expected_config_hash="aaaa1111",
        deployed_config_hash="bbbb2222",  # stale pre-flash mDNS value
        has_pending_changes=True,
    )
    controller, fired = _flush_controller(device)

    controller._sync_deployed_hash_after_flash("kitchen.yaml")

    assert device.deployed_config_hash == "aaaa1111"
    assert device.has_pending_changes is False
    # Exactly one DEVICE_UPDATED — the optimistic apply_config_hash
    # path fires it via the existing _on_config_hash_change callback,
    # not in addition to a separate fire from the sync helper.
    assert [t for t, _p in fired] == [EventType.DEVICE_UPDATED]


@pytest.mark.asyncio
async def test_sync_after_flash_no_expected_hash_is_noop() -> None:
    """Without ``expected_config_hash`` we have nothing to pin — fall back to mtime."""
    device = _device(
        expected_config_hash="",
        deployed_config_hash="bbbb2222",
    )
    controller, fired = _flush_controller(device)

    controller._sync_deployed_hash_after_flash("kitchen.yaml")

    # deployed_config_hash isn't touched — the mtime side of
    # compute_has_pending_changes will catch the post-flash state on
    # the next scanner reload.
    assert device.deployed_config_hash == "bbbb2222"
    assert fired == []


@pytest.mark.asyncio
async def test_sync_after_flash_already_in_sync_is_noop() -> None:
    """Already-matching hashes skip both the cache write and the event."""
    device = _device(
        expected_config_hash="aaaa1111",
        deployed_config_hash="aaaa1111",
        has_pending_changes=False,
    )
    controller, fired = _flush_controller(device)

    controller._sync_deployed_hash_after_flash("kitchen.yaml")

    # apply_config_hash is still called (it's the integration point),
    # but its de-dup short-circuits — no callback, no event, no churn.
    assert fired == []


@pytest.mark.asyncio
async def test_sync_after_flash_unknown_configuration_is_noop() -> None:
    """Configuration not in the scanner's device list — silently skip."""
    device = _device(
        configuration="livingroom.yaml",
        expected_config_hash="aaaa1111",
        deployed_config_hash="bbbb2222",
    )
    controller, fired = _flush_controller(device)

    controller._sync_deployed_hash_after_flash("kitchen.yaml")

    assert device.deployed_config_hash == "bbbb2222"  # unchanged
    assert fired == []
