"""Tests for mDNS-driven running-config-hash sync.

esphome/esphome#16145 added an 8-char lowercase-hex ``config_hash``
TXT record to the ``_esphomelib._tcp`` mDNS service so dashboards can
distinguish "device is running the YAML I just compiled" from "device
is on stale firmware". Mirrors the ``version`` TXT pipeline already
covered by ``test_mdns_version.py`` — we plumb the new TXT through
the same monitor → controller path so the comparison logic that lands
later only has to read ``device.runtime_state.deployed_config_hash``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import Device, EventType

from .conftest import (
    make_device,
    make_devices_controller_with_bus,
    make_state_monitor_with_callbacks,
)


def _device(**overrides: Any) -> Device:
    overrides.setdefault("current_version", "2026.5.0")
    return make_device(**overrides)


# ----------------------------------------------------------------------
# DeviceStateMonitor.apply_config_hash
# ----------------------------------------------------------------------


def test_apply_config_hash_first_observation_fires_callback() -> None:
    """A hash we haven't seen before reaches the controller."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_config_hash("kitchen", "1a2b3c4d") is True
    assert callbacks.calls == [("on_config_hash_change", "kitchen", "1a2b3c4d")]


def test_apply_config_hash_dedupes_same_value() -> None:
    """Same hash twice → callback only fires once.

    mDNS announcements are noisy and the hash only changes when the
    user re-flashes; deduping keeps the DEVICE_UPDATED stream quiet.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_config_hash("kitchen", "1a2b3c4d")
    monitor.apply_config_hash("kitchen", "1a2b3c4d")
    assert callbacks.calls == [("on_config_hash_change", "kitchen", "1a2b3c4d")]


def test_apply_config_hash_fires_on_change() -> None:
    """A different hash than the last observation fires the callback again."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_config_hash("kitchen", "1a2b3c4d")
    monitor.apply_config_hash("kitchen", "deadbeef")
    assert callbacks.calls == [
        ("on_config_hash_change", "kitchen", "1a2b3c4d"),
        ("on_config_hash_change", "kitchen", "deadbeef"),
    ]


def test_apply_config_hash_ignores_empty_string() -> None:
    """Pre-#16145 firmware doesn't broadcast the TXT → empty-string is a no-op."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_config_hash("kitchen", "") is False
    assert callbacks.calls == []


def test_apply_config_hash_refires_after_device_rebuild() -> None:
    """A rebuilt Device with empty hash gets repopulated by the next mDNS event.

    Atomic-write editors (vscode-on-macOS et al.) can briefly remove
    the YAML file mid-save, causing the scanner to fire REMOVED then
    re-ADD with ``previous=None`` — the new Device has
    ``deployed_config_hash=""`` even though zeroconf still has the
    same TXT cached. With the old monitor-side dedupe dict, the next
    mDNS announcement short-circuited because the cache still held
    the value, leaving the drawer's "Deployed" hash stuck on an
    em-dash forever (until either a re-flash or a hash change).
    Deduping against the device's actual field instead means the
    rebuild's empty value is observable, the next announcement fires
    again, and the device repopulates.
    """
    devices = [_device()]
    monitor, callbacks = make_state_monitor_with_callbacks(devices)

    # First observation: device populated.
    monitor.apply_config_hash("kitchen", "1a2b3c4d")
    assert devices[0].runtime_state.deployed_config_hash == "1a2b3c4d"

    # Simulate a scanner rebuild with previous=None: the new Device
    # carries no monitor-derived state.
    devices[0] = _device()
    assert devices[0].runtime_state.deployed_config_hash == ""

    # Same hash arrives again. The monitor must NOT short-circuit on
    # the prior observation; the rebuilt device needs the value back.
    monitor.apply_config_hash("kitchen", "1a2b3c4d")
    assert devices[0].runtime_state.deployed_config_hash == "1a2b3c4d"
    assert [c for c in callbacks.calls if c[0] == "on_config_hash_change"] == [
        ("on_config_hash_change", "kitchen", "1a2b3c4d"),
        ("on_config_hash_change", "kitchen", "1a2b3c4d"),
    ]


def test_apply_config_hash_ignores_unknown_device() -> None:
    """Stray mDNS announcements for devices not in the catalog are dropped."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_config_hash("ghost", "1a2b3c4d") is False
    assert callbacks.calls == []


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


async def test_on_config_hash_change_updates_device_and_fires_event() -> None:
    """The full pipe: callback updates the in-memory device + fires DEVICE_UPDATED."""
    device = _device(deployed_config_hash="")
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_config_hash_change("kitchen", "1a2b3c4d")

    assert device.runtime_state.deployed_config_hash == "1a2b3c4d"
    assert any(e.event_type == EventType.DEVICE_UPDATED for e in captured)


async def test_on_config_hash_change_skips_when_same() -> None:
    """No-op when in-memory device already has the announced hash."""
    device = _device(deployed_config_hash="1a2b3c4d")
    controller, captured = make_devices_controller_with_bus([device])

    controller._on_config_hash_change("kitchen", "1a2b3c4d")

    assert captured == []


async def test_on_config_hash_change_unknown_device_is_noop() -> None:
    """A stray callback for an unknown device must not raise or fire events."""
    controller, captured = make_devices_controller_with_bus([])

    controller._on_config_hash_change("ghost", "1a2b3c4d")

    assert captured == []


async def test_on_config_hash_change_flips_pending_when_hashes_diverge() -> None:
    """Hashes don't match → ``has_pending_changes`` flips True."""
    device = _device(
        expected_config_hash="abc12345",
        deployed_config_hash="",
        has_pending_changes=False,
    )
    controller, _captured = make_devices_controller_with_bus([device])

    controller._on_config_hash_change("kitchen", "deadbeef")

    assert device.runtime_state.deployed_config_hash == "deadbeef"
    assert device.has_pending_changes is True
    # The verdict came from the hash compare, so the frontend gates it on mDNS.
    assert device.pending_changes_via_hash is True


async def test_on_config_hash_change_marks_in_sync_when_hashes_match() -> None:
    """Hashes match → ``has_pending_changes`` flips False."""
    device = _device(
        expected_config_hash="abc12345",
        deployed_config_hash="",
        has_pending_changes=True,
        pending_changes_via_hash=True,
    )
    controller, _captured = make_devices_controller_with_bus([device])

    controller._on_config_hash_change("kitchen", "abc12345")

    assert device.runtime_state.deployed_config_hash == "abc12345"
    assert device.has_pending_changes is False
    assert device.pending_changes_via_hash is False


async def test_on_config_hash_change_leaves_pending_alone_without_expected_hash() -> None:
    """No expected hash on file → don't touch has_pending_changes (mtime fallback owns it)."""
    device = _device(
        expected_config_hash="",
        deployed_config_hash="",
        has_pending_changes=True,
    )
    controller, _captured = make_devices_controller_with_bus([device])

    controller._on_config_hash_change("kitchen", "deadbeef")

    assert device.runtime_state.deployed_config_hash == "deadbeef"
    # Stays as the scanner's last computation; the callback only takes
    # over when both hashes are known.
    assert device.has_pending_changes is True
