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

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.models import Device, EventType

from .conftest import (
    close_scheduled_coro,
    make_device,
    make_devices_controller_with_bus,
    make_state_monitor_with_callbacks,
    record_scheduled_coros,
)


def _device(**overrides: Any) -> Device:
    overrides.setdefault("current_version", "2026.5.0")
    return make_device(**overrides)


# ----------------------------------------------------------------------
# DeviceStateMonitor.apply_version
# ----------------------------------------------------------------------


def test_apply_version_first_observation_fires_callback() -> None:
    """A version we haven't seen before reaches the controller."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_version("kitchen", "2026.5.0") is True
    assert callbacks.calls == [("on_version_change", "kitchen", "2026.5.0")]


def test_apply_version_dedupes_same_value() -> None:
    """Same version twice → callback only fires once.

    mDNS announcements are noisy (state changes, periodic refreshes) so
    deduplication is the difference between a quiet ``DEVICE_UPDATED``
    stream and the UI thrashing.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_version("kitchen", "2026.5.0")
    monitor.apply_version("kitchen", "2026.5.0")
    assert callbacks.calls == [("on_version_change", "kitchen", "2026.5.0")]


def test_apply_version_fires_on_change() -> None:
    """A different version than the last observation fires the callback again."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    monitor.apply_version("kitchen", "2026.5.0")
    monitor.apply_version("kitchen", "2026.6.0")
    assert callbacks.calls == [
        ("on_version_change", "kitchen", "2026.5.0"),
        ("on_version_change", "kitchen", "2026.6.0"),
    ]


def test_apply_version_ignores_empty_string() -> None:
    """Devices that don't announce a version → no-op (don't fire empty-string callbacks)."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_version("kitchen", "") is False
    assert callbacks.calls == []


def test_apply_version_ignores_unknown_device() -> None:
    """Stray mDNS announcements for devices not in the catalog are dropped."""
    monitor, callbacks = make_state_monitor_with_callbacks([_device()])
    assert monitor.apply_version("ghost", "2026.5.0") is False
    assert callbacks.calls == []


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
# DevicesController._on_version_change
# ----------------------------------------------------------------------


async def test_on_version_change_updates_device_fires_event_and_persists(
    monkeypatch: Any,
) -> None:
    """The full pipe: callback updates device, persists to store, fires DEVICE_UPDATED."""
    device = _device(deployed_version="2026.4.0")
    controller, captured = make_devices_controller_with_bus(
        [device], create_background_task=close_scheduled_coro
    )

    controller._on_version_change("kitchen", "2026.5.0")

    assert device.deployed_version == "2026.5.0"
    # current_version is "2026.5.0" too, so update_available should be False.
    assert device.update_available is False
    assert any(e.event_type == EventType.DEVICE_UPDATED for e in captured)
    # A runtime mDNS tick must not look like a YAML edit, or version history
    # would commit (and contend for the git index) on every announce.
    assert not any(e.event_type == EventType.DEVICE_YAML_UPDATED for e in captured)
    # Persisted to the store (deployed_version is a STORE_FIELDS member).
    assert controller._metadata_store.get(device.configuration) == {"deployed_version": "2026.5.0"}


async def test_on_version_change_skips_when_same() -> None:
    """No-op when in-memory device already has the announced version."""
    device = _device(deployed_version="2026.5.0")
    scheduled: list[object] = []
    controller, captured = make_devices_controller_with_bus(
        [device], create_background_task=record_scheduled_coros(scheduled)
    )

    controller._on_version_change("kitchen", "2026.5.0")

    assert captured == []
    assert scheduled == []


async def test_on_version_change_marks_update_available_when_behind() -> None:
    """A device on an older version than the dashboard → ``update_available`` flips on."""
    device = _device(current_version="2026.5.0", deployed_version="2026.4.0")
    controller, _captured = make_devices_controller_with_bus(
        [device], create_background_task=close_scheduled_coro
    )

    # Simulate mDNS reporting an even older version than the previous
    # deployed_version — the dashboard's installed esphome is newer
    # than what's on the device, so an update is available.
    controller._on_version_change("kitchen", "2026.3.0")

    assert device.deployed_version == "2026.3.0"
    assert device.update_available is True
