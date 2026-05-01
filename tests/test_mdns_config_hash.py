"""Tests for mDNS-driven running-config-hash sync.

esphome/esphome#16145 added an 8-char lowercase-hex ``config_hash``
TXT record to the ``_esphomelib._tcp`` mDNS service so dashboards can
distinguish "device is running the YAML I just compiled" from "device
is on stale firmware". Mirrors the ``version`` TXT pipeline already
covered by ``test_mdns_version.py`` — we plumb the new TXT through
the same monitor → controller path so the comparison logic that lands
later only has to read ``device.deployed_config_hash``.
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
    on_config_hash = MagicMock()
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_version_change=MagicMock(),
        on_config_hash_change=on_config_hash,
    )
    return monitor, on_config_hash


# ----------------------------------------------------------------------
# DeviceStateMonitor.apply_config_hash
# ----------------------------------------------------------------------


def test_apply_config_hash_first_observation_fires_callback() -> None:
    """A hash we haven't seen before reaches the controller."""
    monitor, cb = _monitor([_device()])
    assert monitor.apply_config_hash("kitchen", "1a2b3c4d") is True
    cb.assert_called_once_with("kitchen", "1a2b3c4d")


def test_apply_config_hash_dedupes_same_value() -> None:
    """Same hash twice → callback only fires once.

    mDNS announcements are noisy and the hash only changes when the
    user re-flashes; deduping keeps the DEVICE_UPDATED stream quiet.
    """
    monitor, cb = _monitor([_device()])
    monitor.apply_config_hash("kitchen", "1a2b3c4d")
    monitor.apply_config_hash("kitchen", "1a2b3c4d")
    cb.assert_called_once()


def test_apply_config_hash_fires_on_change() -> None:
    """A different hash than the last observation fires the callback again."""
    monitor, cb = _monitor([_device()])
    monitor.apply_config_hash("kitchen", "1a2b3c4d")
    monitor.apply_config_hash("kitchen", "deadbeef")
    assert cb.call_args_list == [
        (("kitchen", "1a2b3c4d"),),
        (("kitchen", "deadbeef"),),
    ]


def test_apply_config_hash_ignores_empty_string() -> None:
    """Pre-#16145 firmware doesn't broadcast the TXT → empty-string is a no-op."""
    monitor, cb = _monitor([_device()])
    assert monitor.apply_config_hash("kitchen", "") is False
    cb.assert_not_called()


def test_apply_config_hash_ignores_unknown_device() -> None:
    """Stray mDNS announcements for devices not in the catalog are dropped."""
    monitor, cb = _monitor([_device()])
    assert monitor.apply_config_hash("ghost", "1a2b3c4d") is False
    cb.assert_not_called()


def test_apply_config_hash_no_callback_silently_drops() -> None:
    """Without a wired callback (older test setups) we don't raise."""
    monitor = DeviceStateMonitor(
        get_devices=lambda: [_device()],
        on_state_change=MagicMock(),
        on_ip_change=MagicMock(),
        on_version_change=None,
        on_config_hash_change=None,
    )
    assert monitor.apply_config_hash("kitchen", "1a2b3c4d") is False


# ----------------------------------------------------------------------
# DevicesController._on_config_hash_change
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_config_hash_change_updates_device_and_fires_event() -> None:
    """The full pipe: callback updates the in-memory device + fires DEVICE_UPDATED."""
    from esphome_device_builder.controllers.devices import DevicesController
    from esphome_device_builder.models import EventType

    device = _device(deployed_config_hash="")

    db = MagicMock()
    fired_events: list[tuple[EventType, dict]] = []
    db.bus.fire.side_effect = lambda event_type, data: fired_events.append((event_type, data))

    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = [device]

    controller._on_config_hash_change("kitchen", "1a2b3c4d")

    assert device.deployed_config_hash == "1a2b3c4d"
    assert any(et == EventType.DEVICE_UPDATED for et, _ in fired_events)


@pytest.mark.asyncio
async def test_on_config_hash_change_skips_when_same() -> None:
    """No-op when in-memory device already has the announced hash."""
    from esphome_device_builder.controllers.devices import DevicesController

    device = _device(deployed_config_hash="1a2b3c4d")

    db = MagicMock()
    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = [device]

    controller._on_config_hash_change("kitchen", "1a2b3c4d")

    db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_on_config_hash_change_unknown_device_is_noop() -> None:
    """A stray callback for an unknown device must not raise or fire events."""
    from esphome_device_builder.controllers.devices import DevicesController

    db = MagicMock()
    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = []

    controller._on_config_hash_change("ghost", "1a2b3c4d")

    db.bus.fire.assert_not_called()


@pytest.mark.asyncio
async def test_on_config_hash_change_flips_pending_when_hashes_diverge() -> None:
    """Hashes don't match → ``has_pending_changes`` flips True."""
    from esphome_device_builder.controllers.devices import DevicesController

    device = _device(
        expected_config_hash="abc12345",
        deployed_config_hash="",
        has_pending_changes=False,
    )

    db = MagicMock()
    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = [device]

    controller._on_config_hash_change("kitchen", "deadbeef")

    assert device.deployed_config_hash == "deadbeef"
    assert device.has_pending_changes is True


@pytest.mark.asyncio
async def test_on_config_hash_change_marks_in_sync_when_hashes_match() -> None:
    """Hashes match → ``has_pending_changes`` flips False."""
    from esphome_device_builder.controllers.devices import DevicesController

    device = _device(
        expected_config_hash="abc12345",
        deployed_config_hash="",
        has_pending_changes=True,
    )

    db = MagicMock()
    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = [device]

    controller._on_config_hash_change("kitchen", "abc12345")

    assert device.deployed_config_hash == "abc12345"
    assert device.has_pending_changes is False


@pytest.mark.asyncio
async def test_on_config_hash_change_leaves_pending_alone_without_expected_hash() -> None:
    """No expected hash on file → don't touch has_pending_changes (mtime fallback owns it)."""
    from esphome_device_builder.controllers.devices import DevicesController

    device = _device(
        expected_config_hash="",
        deployed_config_hash="",
        has_pending_changes=True,
    )

    db = MagicMock()
    controller = DevicesController.__new__(DevicesController)
    controller._db = db
    controller._scanner = MagicMock()
    controller._scanner.devices = [device]

    controller._on_config_hash_change("kitchen", "deadbeef")

    assert device.deployed_config_hash == "deadbeef"
    # Stays as the scanner's last computation; the callback only takes
    # over when both hashes are known.
    assert device.has_pending_changes is True
