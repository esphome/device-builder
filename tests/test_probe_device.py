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
from esphome_device_builder.controllers._device_state_monitor._state import MonitorState
from esphome_device_builder.controllers._device_state_monitor.importable import ImportableDiscovery
from esphome_device_builder.controllers._device_state_monitor.mdns import MdnsSource
from esphome_device_builder.controllers._device_state_monitor.ping import PingSource
from esphome_device_builder.models import Device, DeviceState

from .conftest import RecordingMonitorCallbacks


def _make_monitor() -> DeviceStateMonitor:
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)

    monitor.state = MonitorState()

    monitor._importable = ImportableDiscovery(monitor)

    monitor._mdns = MdnsSource(monitor)

    monitor._presence = None
    monitor._ping = PingSource(monitor)
    monitor._mdns._zeroconf = MagicMock()
    monitor._mdns._zeroconf.zeroconf = MagicMock()
    monitor._tasks = set()
    monitor.state.reachability = None
    return monitor


def _capture_apply(
    monitor: DeviceStateMonitor, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, Any]]:
    """Swap ``_apply_service_info`` for a list-append closure and return the log.

    A typed substitute for the previous ``apply = MagicMock();
    apply.assert_called_once_with(...)`` shape. The win is on the
    *assertion-method* side: against a ``MagicMock`` a typo'd
    method name (``apply.assertt_called_once_with``) silently
    passes because ``MagicMock`` spawns a fresh attribute on
    access. Comparing the returned list with ``==`` has no
    ``assert_*`` method to misspell — the failure mode is a clear
    diff at the comparison instead of a vacuous green.
    """
    calls: list[tuple[str, Any]] = []

    def _apply(name: str, info: Any) -> None:
        calls.append((name, info))

    monkeypatch.setattr(monitor._mdns, "_apply_service_info", _apply)
    return calls


@pytest.mark.asyncio
async def test_probe_device_cache_hit_applies_synchronously(monkeypatch) -> None:
    """A cached service info applies inline; no task is spawned."""
    monitor = _make_monitor()
    apply_calls = _capture_apply(monitor, monkeypatch)

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.importable.AsyncServiceInfo",
        lambda *_args, **_kw: fake_info,
    )

    monitor.probe_device("kitchen")

    assert apply_calls == [("kitchen", fake_info)]
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
    apply_calls = _capture_apply(monitor, monkeypatch)

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = True
    constructor_args: list[tuple[Any, ...]] = []

    def _info_ctor(service_type: Any, full_service: Any) -> Any:
        constructor_args.append((service_type, full_service))
        return fake_info

    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.importable.AsyncServiceInfo",
        _info_ctor,
    )

    monitor.probe_device("my-living-room", service_name="apollo-r-pro-1-eth-5938e0")

    # Looked up under the OLD broadcast name…
    assert constructor_args == [
        ("_esphomelib._tcp.local.", "apollo-r-pro-1-eth-5938e0._esphomelib._tcp.local.")
    ]
    # …but applied under the NEW configured name.
    assert apply_calls == [("my-living-room", fake_info)]


@pytest.mark.asyncio
async def test_probe_device_cache_miss_spawns_task(monkeypatch) -> None:
    """Cache miss → fire-and-forget resolve task tracked in ``_tasks``."""
    monitor = _make_monitor()
    apply_calls = _capture_apply(monitor, monkeypatch)

    fake_info = MagicMock()
    fake_info.load_from_cache.return_value = False
    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.importable.AsyncServiceInfo",
        lambda *_args, **_kw: fake_info,
    )

    async def fake_resolve(*_args, **_kw) -> None:
        return None

    monkeypatch.setattr(monitor._mdns, "_resolve_and_apply", fake_resolve)

    monitor.probe_device("kitchen")

    assert apply_calls == []
    # One task was registered for tracking. Wait it out so the
    # done-callback can run and prune the set; otherwise pending
    # tasks would leak into the next test's event loop.
    assert len(monitor._tasks) == 1
    await asyncio.gather(*monitor._tasks)


def test_probe_device_no_zeroconf_is_a_noop() -> None:
    """Pre-start (or zeroconf-failed) probe must not raise."""
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)

    monitor.state = MonitorState()

    monitor._importable = ImportableDiscovery(monitor)

    monitor._mdns = MdnsSource(monitor)

    monitor._presence = None
    monitor._ping = PingSource(monitor)
    monitor._mdns._zeroconf = None
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
    monitor.state.state_source = {}
    # ``apply()`` validates against the configured-devices catalog.
    device = Device(
        name="kitchen",
        friendly_name="Kitchen",
        configuration="kitchen.yaml",
        address="kitchen.local",
        state=DeviceState.UNKNOWN,
    )
    monitor._get_devices = lambda: [device]
    monitor._get_devices_by_name = lambda name: [device] if device.name == name else []
    callbacks = RecordingMonitorCallbacks([device])
    monitor._on_state_change = callbacks.on_state_change
    monitor._on_ip_change = callbacks.on_ip_change
    monitor._on_version_change = callbacks.on_version_change
    monitor._on_config_hash_change = callbacks.on_config_hash_change
    monitor._on_api_encryption_change = callbacks.on_api_encryption_change
    monitor.state.reachability = None

    fake_info = MagicMock()
    fake_info.parsed_scoped_addresses.return_value = []
    fake_info.decoded_properties = {}
    monitor._mdns._apply_service_info("kitchen", fake_info)

    # ``_on_state_change`` is the bridge our owner registered for
    # state transitions; the call carries (name, state, source).
    assert callbacks.calls_for("on_state_change") == [
        ("on_state_change", "kitchen", DeviceState.ONLINE, "mdns")
    ]


@pytest.mark.asyncio
async def test_apply_service_info_routes_mac_txt_to_apply_mac_address() -> None:
    """A populated ``mac`` TXT lands at ``apply_mac_address``.

    Pins the wiring at the TXT-extraction site
    (``_apply_service_info_to_device``) where the broadcast value
    is plucked out of ``decoded_properties`` alongside ``version`` /
    ``config_hash``. Without this hop the canonical-form
    normalization + dedupe never runs and the drawer / sidecar
    don't get populated from the broadcast.
    """
    monitor = _make_monitor()
    monitor.state.state_source = {}
    device = Device(
        name="kitchen",
        friendly_name="Kitchen",
        configuration="kitchen.yaml",
        address="kitchen.local",
        state=DeviceState.UNKNOWN,
    )
    monitor._get_devices = lambda: [device]
    monitor._get_devices_by_name = lambda name: [device] if device.name == name else []
    callbacks = RecordingMonitorCallbacks([device])
    monitor._on_state_change = callbacks.on_state_change
    monitor._on_ip_change = callbacks.on_ip_change
    monitor._on_version_change = callbacks.on_version_change
    monitor._on_config_hash_change = callbacks.on_config_hash_change
    monitor._on_api_encryption_change = callbacks.on_api_encryption_change
    monitor._on_mac_address_change = callbacks.on_mac_address_change
    monitor.state.reachability = None

    fake_info = MagicMock()
    fake_info.parsed_scoped_addresses.return_value = []
    # Wire-form value (lowercase 12-hex-char, no separators) — what
    # ESPHome firmware actually broadcasts. The callback receives
    # the canonical form because ``apply_mac_address`` normalizes
    # before invoking the change callback.
    fake_info.decoded_properties = {"mac": "94c9601f8cf1"}
    monitor._mdns._apply_service_info("kitchen", fake_info)

    assert callbacks.calls_for("on_mac_address_change") == [
        ("on_mac_address_change", "kitchen", "94:C9:60:1F:8C:F1")
    ]
