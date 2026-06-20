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

import asyncio
import tempfile as _tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from esphome.core import CORE

from esphome_device_builder.controllers._device_scanner import (
    DeviceFileMetadata,
    DeviceScanner,
    ScanChange,
)
from esphome_device_builder.controllers.devices import DevicesController, firmware_sync
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
from tests._storage_fixtures import write_storage_json
from tests.conftest import make_device


def _device(name: str = "kitchen", **overrides: Any) -> Device:
    overrides.setdefault("current_version", "2026.5.0")
    return make_device(name=name, **overrides)


# ----------------------------------------------------------------------
# DeviceScanner.reload
# ----------------------------------------------------------------------


async def test_reload_rereads_state_and_fires_reloaded(tmp_path: Path) -> None:
    """Reload re-runs the loader and fires ``RELOADED`` so listeners refresh."""
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
    assert changes == [(ScanChange.RELOADED, refreshed)]


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
    controller._state_monitor = MagicMock()
    # The flashed branch arms a post-flash re-probe timer.
    controller._reprobe_timers = {}

    await controller._refresh_after_firmware_job("kitchen.yaml", recompute_hash=False, flashed=True)

    assert compute_calls == []
    assert controller._scanner.calls == [("reload", "kitchen.yaml")]
    controller._cancel_reprobe_timers()


# ----------------------------------------------------------------------
# DevicesController._sync_deployed_state_after_flash
#
# Drives the badges-clear-after-OTA fix. The reloaded device inherits
# ``deployed_config_hash`` / ``deployed_version`` from the previous
# in-memory snapshot — typically the now-stale pre-flash values — so
# without this sync ``has_pending_changes`` and ``update_available``
# read stale and the user sees a still-orange dot / "update available"
# until the rebooted device's mDNS announce propagates (seconds at
# best, "never" in mDNS-dark deployments like the Docker bridge).
# ----------------------------------------------------------------------

_VERSION_READ = (
    "esphome_device_builder.controllers.devices.firmware_sync._read_compiled_esphome_version"
)


def _flush_controller(device: Device) -> tuple[Any, list[Any]]:
    """Build a controller seeded with *device* and a fired-events list."""
    fired: list[Any] = []

    db = MagicMock()
    db.bus.fire.side_effect = lambda event_type, payload: fired.append((event_type, payload))

    scanner = MagicMock()
    scanner.devices = [device]
    scanner.get_by_name = lambda name: [device] if device.name == name else []
    scanner.get_by_configuration = lambda cfg: device if device.configuration == cfg else None

    state_monitor = MagicMock()
    # Drive the same de-dup behaviour the real ``apply_*`` methods have —
    # if the scan device's name matches and the value differs from the
    # cached one, fire the controller's ``_on_*_change`` callback so the
    # assertion can verify the device fields flipped.
    hash_cache: dict[str, str] = {}
    version_cache: dict[str, str] = {}

    def _apply_hash(name: str, config_hash: str) -> bool:
        if not config_hash or hash_cache.get(name) == config_hash:
            return False
        hash_cache[name] = config_hash
        controller._on_config_hash_change(name, config_hash)
        return True

    def _apply_version(name: str, version: str) -> bool:
        if not version or version_cache.get(name) == version:
            return False
        version_cache[name] = version
        controller._on_version_change(name, version)
        return True

    state_monitor.apply_config_hash.side_effect = _apply_hash
    state_monitor.apply_version.side_effect = _apply_version

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


async def test_sync_after_flash_pins_hash_and_version(monkeypatch: Any) -> None:
    """Post-flash sync flips both deployed hash + version and clears both badges."""
    monkeypatch.setattr(_VERSION_READ, lambda _cfg: "2026.6.2")
    device = _device(
        expected_config_hash="aaaa1111",
        deployed_config_hash="bbbb2222",  # stale pre-flash mDNS value
        deployed_version="2024.6.4",  # stale pre-flash version
        current_version="2026.6.2",
        has_pending_changes=True,
        update_available=True,
    )
    controller, fired = _flush_controller(device)

    await controller._sync_deployed_state_after_flash("kitchen.yaml")

    assert device.deployed_config_hash == "aaaa1111"
    assert device.has_pending_changes is False
    assert device.deployed_version == "2026.6.2"
    assert device.update_available is False
    # One DEVICE_UPDATED per pin, via the existing _on_*_change callbacks.
    assert [t for t, _p in fired] == [EventType.DEVICE_UPDATED, EventType.DEVICE_UPDATED]


async def test_sync_after_flash_no_expected_hash_pins_version_only(monkeypatch: Any) -> None:
    """Without ``expected_config_hash`` the hash pin is skipped; the version still pins."""
    monkeypatch.setattr(_VERSION_READ, lambda _cfg: "2026.6.2")
    device = _device(
        expected_config_hash="",
        deployed_config_hash="bbbb2222",
        deployed_version="2024.6.4",
        current_version="2026.6.2",
    )
    controller, fired = _flush_controller(device)

    await controller._sync_deployed_state_after_flash("kitchen.yaml")

    assert device.deployed_config_hash == "bbbb2222"  # untouched
    assert device.deployed_version == "2026.6.2"
    assert [t for t, _p in fired] == [EventType.DEVICE_UPDATED]


async def test_sync_after_flash_no_compiled_version_pins_hash_only(monkeypatch: Any) -> None:
    """Without a StorageJSON version the version pin is skipped; the hash still pins."""
    monkeypatch.setattr(_VERSION_READ, lambda _cfg: "")
    device = _device(
        expected_config_hash="aaaa1111",
        deployed_config_hash="bbbb2222",
        deployed_version="2024.6.4",
        has_pending_changes=True,
    )
    controller, fired = _flush_controller(device)

    await controller._sync_deployed_state_after_flash("kitchen.yaml")

    assert device.deployed_config_hash == "aaaa1111"
    assert device.has_pending_changes is False
    assert device.deployed_version == "2024.6.4"  # untouched
    assert [t for t, _p in fired] == [EventType.DEVICE_UPDATED]


async def test_sync_after_flash_already_in_sync_is_noop(monkeypatch: Any) -> None:
    """Already-matching hash + version skip both the cache write and the event."""
    monkeypatch.setattr(_VERSION_READ, lambda _cfg: "2026.6.2")
    device = _device(
        expected_config_hash="aaaa1111",
        deployed_config_hash="aaaa1111",
        deployed_version="2026.6.2",
        current_version="2026.6.2",
        has_pending_changes=False,
    )
    controller, fired = _flush_controller(device)

    await controller._sync_deployed_state_after_flash("kitchen.yaml")

    # The apply_* methods are still called (they're the integration point),
    # but their de-dup short-circuits — no callback, no event, no churn.
    assert fired == []


async def test_sync_after_flash_unknown_configuration_is_noop(monkeypatch: Any) -> None:
    """Configuration not in the scanner's device list — silently skip."""
    monkeypatch.setattr(_VERSION_READ, lambda _cfg: "2026.6.2")
    device = _device(
        configuration="livingroom.yaml",
        expected_config_hash="aaaa1111",
        deployed_config_hash="bbbb2222",
        deployed_version="2024.6.4",
    )
    controller, fired = _flush_controller(device)

    await controller._sync_deployed_state_after_flash("kitchen.yaml")

    assert device.deployed_config_hash == "bbbb2222"  # unchanged
    assert device.deployed_version == "2024.6.4"  # unchanged
    assert fired == []


# ----------------------------------------------------------------------
# Post-flash version re-probe timer
#
# A successful flash optimistically pins the version, but in an mDNS-dark
# deployment the dashboard can't see a rollback / failed boot. A one-shot
# Native-API re-probe armed ~60s after the device reboots confirms (or
# corrects) the running version. Scheduled via ``loop.call_later`` and
# tracked so ``stop`` can cancel anything still pending.
# ----------------------------------------------------------------------

_DELAY = (
    "esphome_device_builder.controllers.devices.firmware_sync._POST_FLASH_VERSION_REPROBE_DELAY"
)


def _reprobe_controller(device: Device | None) -> Any:
    """Build a bare controller wired for the re-probe timer path."""
    controller = DevicesController.__new__(DevicesController)
    controller._reprobe_timers = {}
    controller._scanner = MagicMock()
    controller._scanner.get_by_configuration = lambda cfg: (
        device if device is not None and device.configuration == cfg else None
    )
    controller._state_monitor = MagicMock()
    return controller


async def test_schedule_version_reprobe_fires_and_requests(monkeypatch: Any) -> None:
    """When the timer fires, the device's name is handed to the monitor's re-probe."""
    monkeypatch.setattr(_DELAY, 0)
    device = _device()
    controller = _reprobe_controller(device)

    controller._schedule_version_reprobe("kitchen.yaml")
    await asyncio.sleep(0.01)  # let the call_later(0) timer fire

    controller._state_monitor.request_version_reprobe.assert_called_once_with(device.name)
    assert controller._reprobe_timers == {}  # consumed


async def test_fire_version_reprobe_unknown_configuration_is_noop() -> None:
    """No matching device when the timer fires → nothing to re-probe."""
    controller = _reprobe_controller(None)

    firmware_sync._fire_version_reprobe(controller, "kitchen.yaml")

    controller._state_monitor.request_version_reprobe.assert_not_called()


async def test_schedule_version_reprobe_reschedule_cancels_previous(monkeypatch: Any) -> None:
    """Re-arming for the same configuration cancels the prior pending timer."""
    monkeypatch.setattr(_DELAY, 60)  # long enough that neither fires during the test
    controller = _reprobe_controller(_device())

    controller._schedule_version_reprobe("kitchen.yaml")
    first = controller._reprobe_timers["kitchen.yaml"]
    controller._schedule_version_reprobe("kitchen.yaml")

    assert first.cancelled()
    assert controller._reprobe_timers["kitchen.yaml"] is not first
    controller._cancel_reprobe_timers()


async def test_cancel_reprobe_timers_cancels_pending(monkeypatch: Any) -> None:
    """``stop`` cancels any armed-but-unfired re-probe timers."""
    monkeypatch.setattr(_DELAY, 60)
    controller = _reprobe_controller(_device())

    controller._schedule_version_reprobe("kitchen.yaml")
    handle = controller._reprobe_timers["kitchen.yaml"]
    controller._cancel_reprobe_timers()

    assert handle.cancelled()
    assert controller._reprobe_timers == {}


# ----------------------------------------------------------------------
# Helpers: _read_compiled_esphome_version + DeviceScanner.get_by_configuration
# ----------------------------------------------------------------------


def test_read_compiled_esphome_version_reads_storage(tmp_path: Path, monkeypatch: Any) -> None:
    """The version is read off the device's StorageJSON sidecar."""
    monkeypatch.setattr(CORE, "config_path", tmp_path / "___sentinel___.yaml")
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"esphome_version": "2026.6.2"})
    assert firmware_sync._read_compiled_esphome_version("kitchen.yaml") == "2026.6.2"


def test_read_compiled_esphome_version_missing_storage_is_empty(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """No sidecar (never compiled / wiped) → empty string, never raise."""
    monkeypatch.setattr(CORE, "config_path", tmp_path / "___sentinel___.yaml")
    assert firmware_sync._read_compiled_esphome_version("ghost.yaml") == ""


def test_read_compiled_esphome_version_blank_version_is_empty(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A sidecar with no esphome_version → empty string."""
    monkeypatch.setattr(CORE, "config_path", tmp_path / "___sentinel___.yaml")
    write_storage_json(tmp_path, "kitchen.yaml", overrides={"esphome_version": None})
    assert firmware_sync._read_compiled_esphome_version("kitchen.yaml") == ""


def test_scanner_get_by_configuration(tmp_path: Path) -> None:
    """The indexed lookup returns the device for a tracked filename, else ``None``."""
    scanner = DeviceScanner(
        config_dir=tmp_path,
        get_metadata=lambda _config_dir, _filename: DeviceFileMetadata(board_id="", ip=""),
        on_change=lambda _kind, _device: None,
    )
    device = _device()
    scanner._index.set(tmp_path / "kitchen.yaml", device, (0, 0, 0.0, 0))

    assert scanner.get_by_configuration("kitchen.yaml") is device
    assert scanner.get_by_configuration("ghost.yaml") is None
