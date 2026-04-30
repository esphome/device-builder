"""Tests for mDNS-driven ESPHome version sync.

When a device broadcasts ``_esphomelib._tcp.local.`` it includes a
``version`` TXT record with the firmware version actually running. The
dashboard pulls that out so the stored ``StorageJSON.esphome_version``
reflects reality, not just whatever the dashboard last compiled —
important after an out-of-band OTA or a flash from another tool.
Mirrors ``DashboardImportDiscovery.update_device_mdns`` in
``esphome/zeroconf.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import Device, DeviceState


def _device(**overrides: Any) -> Device:
    base: dict[str, Any] = {
        "name": "kitchen",
        "friendly_name": "Kitchen",
        "configuration": "kitchen.yaml",
        "address": "kitchen.local",
        "current_version": "2026.5.0",
        "deployed_version": "",
        "state": DeviceState.UNKNOWN,
    }
    base.update(overrides)
    return Device(**base)


def _monitor(devices: list[Device]) -> tuple[DeviceStateMonitor, MagicMock]:
    on_version = MagicMock()
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_version_change=on_version,
    )
    return monitor, on_version


# ----------------------------------------------------------------------
# DeviceStateMonitor.apply_version
# ----------------------------------------------------------------------


def test_apply_version_first_observation_fires_callback() -> None:
    """A version we haven't seen before reaches the controller."""
    monitor, cb = _monitor([_device()])
    assert monitor.apply_version("kitchen", "2026.5.0") is True
    cb.assert_called_once_with("kitchen", "2026.5.0")


def test_apply_version_dedupes_same_value() -> None:
    """Same version twice → callback only fires once.

    mDNS announcements are noisy (state changes, periodic refreshes) so
    deduplication is the difference between a quiet ``DEVICE_UPDATED``
    stream and the UI thrashing.
    """
    monitor, cb = _monitor([_device()])
    monitor.apply_version("kitchen", "2026.5.0")
    monitor.apply_version("kitchen", "2026.5.0")
    cb.assert_called_once()


def test_apply_version_fires_on_change() -> None:
    """A different version than the last observation fires the callback again."""
    monitor, cb = _monitor([_device()])
    monitor.apply_version("kitchen", "2026.5.0")
    monitor.apply_version("kitchen", "2026.6.0")
    assert cb.call_args_list == [
        (("kitchen", "2026.5.0"),),
        (("kitchen", "2026.6.0"),),
    ]


def test_apply_version_ignores_empty_string() -> None:
    """Devices that don't announce a version → no-op (don't fire empty-string callbacks)."""
    monitor, cb = _monitor([_device()])
    assert monitor.apply_version("kitchen", "") is False
    cb.assert_not_called()


def test_apply_version_ignores_unknown_device() -> None:
    """Stray mDNS announcements for devices not in the catalog are dropped."""
    monitor, cb = _monitor([_device()])
    assert monitor.apply_version("ghost", "2026.5.0") is False
    cb.assert_not_called()


def test_apply_version_no_callback_silently_drops() -> None:
    """When no callback was wired (test setups, partial init) we don't raise."""
    monitor = DeviceStateMonitor(
        get_devices=lambda: [_device()],
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_version_change=None,
    )
    assert monitor.apply_version("kitchen", "2026.5.0") is False


# ----------------------------------------------------------------------
# StorageJSON write-through
# ----------------------------------------------------------------------


def _patch_storage(monkeypatch: Any, tmp_path: Any, storage: Any) -> None:
    """Wire ``StorageJSON.load`` and ``ext_storage_path`` for the workhorse tests.

    ``ext_storage_path`` walks ``CORE.config_path`` — without a config
    loaded, it raises before we even get to the mocked load. Pointing it
    at ``tmp_path`` keeps the call inert for the test's duration.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.StorageJSON.load",
        lambda _path: storage,
    )
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.ext_storage_path",
        lambda config: tmp_path / f"{config}.json",
    )


def test_persist_storage_version_writes_when_different(monkeypatch: Any, tmp_path: Any) -> None:
    """``_persist_storage_version`` saves when the on-disk value differs."""
    from esphome_device_builder.controllers.devices import DevicesController

    storage = MagicMock()
    storage.esphome_version = "2026.4.0"
    _patch_storage(monkeypatch, tmp_path, storage)

    DevicesController._persist_storage_version("kitchen.yaml", "2026.5.0")

    assert storage.esphome_version == "2026.5.0"
    storage.save.assert_called_once()


def test_persist_storage_version_skips_when_same(monkeypatch: Any, tmp_path: Any) -> None:
    """No write when on-disk value already matches — prevents touch-mtime churn.

    Without this guard, every mDNS refresh (every few seconds) would
    bump the StorageJSON mtime, defeat the scanner's mtime-based cache,
    and force a full re-parse of every YAML on the next poll.
    """
    from esphome_device_builder.controllers.devices import DevicesController

    storage = MagicMock()
    storage.esphome_version = "2026.5.0"
    _patch_storage(monkeypatch, tmp_path, storage)

    DevicesController._persist_storage_version("kitchen.yaml", "2026.5.0")

    storage.save.assert_not_called()


def test_persist_storage_version_handles_missing_storage(monkeypatch: Any, tmp_path: Any) -> None:
    """Device that's never been compiled has no StorageJSON — bail out cleanly."""
    from esphome_device_builder.controllers.devices import DevicesController

    _patch_storage(monkeypatch, tmp_path, storage=None)

    # Should not raise.
    DevicesController._persist_storage_version("kitchen.yaml", "2026.5.0")


# ----------------------------------------------------------------------
# DevicesController._on_version_change
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_version_change_updates_device_and_fires_event(monkeypatch: Any) -> None:
    """The full pipe: callback updates the in-memory device + fires DEVICE_UPDATED."""
    from esphome_device_builder.controllers.devices import DevicesController
    from esphome_device_builder.models import EventType

    device = _device(deployed_version="2026.4.0")

    db = MagicMock()

    fired_events: list[tuple[EventType, dict]] = []
    db.bus.fire.side_effect = lambda event_type, data: fired_events.append((event_type, data))

    persisted: list[tuple[str, str]] = []

    async def _fake_persist(configuration: str, version: str) -> None:
        persisted.append((configuration, version))

    db.create_background_task.side_effect = lambda coro: coro.close() or MagicMock()

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = [device]
    monkeypatch.setattr(controller, "_persist_storage_version_async", _fake_persist, raising=False)

    controller._on_version_change("kitchen", "2026.5.0")

    assert device.deployed_version == "2026.5.0"
    # current_version is "2026.5.0" too, so update_available should be False.
    assert device.update_available is False
    assert any(et == EventType.DEVICE_UPDATED for et, _ in fired_events)


@pytest.mark.asyncio
async def test_on_version_change_skips_when_same(monkeypatch: Any) -> None:
    """No-op when in-memory device already has the announced version."""
    from esphome_device_builder.controllers.devices import DevicesController

    device = _device(deployed_version="2026.5.0")

    db = MagicMock()

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = [device]

    controller._on_version_change("kitchen", "2026.5.0")

    db.bus.fire.assert_not_called()
    db.create_background_task.assert_not_called()


@pytest.mark.asyncio
async def test_on_version_change_marks_update_available_when_behind() -> None:
    """A device on an older version than the dashboard → ``update_available`` flips on."""
    from esphome_device_builder.controllers.devices import DevicesController

    device = _device(current_version="2026.5.0", deployed_version="2026.4.0")

    db = MagicMock()
    db.create_background_task.side_effect = lambda coro: coro.close() or MagicMock()

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = [device]

    # Simulate mDNS reporting an even older version than the previous
    # deployed_version — the dashboard's installed esphome is newer
    # than what's on the device, so an update is available.
    controller._on_version_change("kitchen", "2026.3.0")

    assert device.deployed_version == "2026.3.0"
    assert device.update_available is True
