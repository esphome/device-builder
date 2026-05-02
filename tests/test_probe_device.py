"""
Tests for ``DeviceStateMonitor.probe_device``.

Adoption / wizard / on-disk YAML drops all need an eager mDNS
probe so the new device card lands fully populated (IP, version,
config_hash, api_encryption) instead of waiting on the next
periodic announcement. The probe takes the cache fast-path when
zeroconf already has the service, otherwise it spawns a fire-
and-forget resolve task.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers._device_state_monitor import (
    DeviceStateMonitor,
)


def _make_monitor() -> DeviceStateMonitor:
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)
    monitor._zeroconf = MagicMock()
    monitor._zeroconf.zeroconf = MagicMock()
    monitor._tasks = set()
    return monitor


@pytest.mark.asyncio
async def test_probe_device_cache_hit_applies_synchronously(monkeypatch) -> None:
    """A cached service info applies inline; no task is spawned."""
    monitor = _make_monitor()
    apply = MagicMock()
    monkeypatch.setattr(monitor, "_apply_service_info", apply)

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.AsyncServiceInfo",
        lambda *_args, **_kw: fake_info,
    )

    monitor.probe_device("kitchen")

    apply.assert_called_once_with("kitchen", fake_info)
    assert not monitor._tasks


@pytest.mark.asyncio
async def test_probe_device_uses_service_name_when_provided(monkeypatch) -> None:
    """``service_name`` overrides the lookup but apply still keys by ``device_name``.

    Adoption surfaces a device whose mDNS-advertised name (the
    factory firmware's hostname) differs from the user-chosen YAML
    name. The probe needs to look up the broadcast under the OLD
    name (which is what zeroconf has cached) but apply the data to
    the configured device under its NEW name.
    """
    monitor = _make_monitor()
    apply = MagicMock()
    monkeypatch.setattr(monitor, "_apply_service_info", apply)

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    constructor_args: list[tuple[Any, ...]] = []

    def _info_ctor(service_type: Any, full_service: Any) -> Any:
        constructor_args.append((service_type, full_service))
        return fake_info

    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.AsyncServiceInfo",
        _info_ctor,
    )

    monitor.probe_device("my-living-room", service_name="apollo-r-pro-1-eth-5938e0")

    # Looked up under the OLD broadcast name…
    assert constructor_args == [
        ("_esphomelib._tcp.local.", "apollo-r-pro-1-eth-5938e0._esphomelib._tcp.local.")
    ]
    # …but applied under the NEW configured name.
    apply.assert_called_once_with("my-living-room", fake_info)


@pytest.mark.asyncio
async def test_probe_device_cache_miss_spawns_task(monkeypatch) -> None:
    """Cache miss → fire-and-forget resolve task tracked in ``_tasks``."""
    monitor = _make_monitor()
    apply = MagicMock()
    monkeypatch.setattr(monitor, "_apply_service_info", apply)

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.AsyncServiceInfo",
        lambda *_args, **_kw: fake_info,
    )

    async def fake_resolve(*_args, **_kw) -> None:
        return None

    monkeypatch.setattr(monitor, "_resolve_and_apply", fake_resolve)

    monitor.probe_device("kitchen")

    apply.assert_not_called()
    # One task was registered for tracking. Wait it out so the
    # done-callback can run and prune the set; otherwise pending
    # tasks would leak into the next test's event loop.
    assert len(monitor._tasks) == 1
    await asyncio.gather(*monitor._tasks)


def test_probe_device_no_zeroconf_is_a_noop() -> None:
    """Pre-start (or zeroconf-failed) probe must not raise."""
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)
    monitor._zeroconf = None
    monitor._tasks = set()

    monitor.probe_device("kitchen")  # no exception, no tasks
    assert not monitor._tasks


@pytest.mark.asyncio
async def test_apply_service_info_claims_online() -> None:
    """``_apply_service_info`` flips the device ONLINE under the mDNS source.

    A successful apply means we have the device's broadcast address
    + TXT records from zeroconf, which is itself proof it's
    reachable. Without this claim the eager probe path could write
    fully-populated TXT data while leaving the card at "Unknown"
    until the next ping sweep.
    """
    monitor = _make_monitor()
    monitor._on_state_change = MagicMock()
    monitor._on_ip_change = MagicMock()
    monitor._on_version_change = MagicMock()
    monitor._on_config_hash_change = MagicMock()
    monitor._on_api_encryption_change = MagicMock()
    monitor._state_source = {}
    # ``apply()`` validates against the configured-devices catalog.
    from esphome_device_builder.models import Device, DeviceState

    device = Device(
        name="kitchen",
        friendly_name="Kitchen",
        configuration="kitchen.yaml",
        address="kitchen.local",
        state=DeviceState.UNKNOWN,
    )
    monitor._get_devices = lambda: [device]
    monitor._get_devices_by_name = lambda name: [device] if device.name == name else []

    fake_info = MagicMock()
    fake_info.parsed_scoped_addresses.return_value = []
    fake_info.decoded_properties = {}
    monitor._apply_service_info("kitchen", fake_info)

    # ``_on_state_change`` is the bridge our owner registered for
    # state transitions; the call carries (name, state, source).
    monitor._on_state_change.assert_called_once_with("kitchen", DeviceState.ONLINE, "mdns")
