"""Malformed mDNS service-instance names are dropped before any handler runs (#2620)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from zeroconf import ServiceStateChange

from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.controllers._device_state_monitor._state import MonitorState
from esphome_device_builder.controllers._device_state_monitor.importable import ImportableDiscovery
from esphome_device_builder.controllers._device_state_monitor.mdns import MdnsSource
from esphome_device_builder.helpers.hostname import valid_mdns_service_name

_ESPHOME = "_esphomelib._tcp.local."
_HTTP = "_http._tcp.local."

# The reporter's IRIS alarm module: control characters in the instance label.
_BAD_HTTP = f"IRIS\x10µA \x10½pG.{_HTTP}"
_BAD_ESPHOME = f"IRIS\x10evil.{_ESPHOME}"

_ALL_CHANGES = (
    ServiceStateChange.Added,
    ServiceStateChange.Updated,
    ServiceStateChange.Removed,
)


def _make_monitor() -> DeviceStateMonitor:
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)
    monitor.state = MonitorState()
    monitor.importable = ImportableDiscovery(monitor)
    monitor.importable.setup()
    monitor.mdns = MdnsSource(monitor)
    monitor._tasks = set()
    monitor._get_devices_by_name = lambda _name: []
    monitor._find_device_by_name = lambda _name: None
    return monitor


def test_valid_mdns_service_name() -> None:
    assert valid_mdns_service_name(f"klo.{_ESPHOME}")
    assert valid_mdns_service_name(f"My Printer (2).{_HTTP}")
    assert not valid_mdns_service_name(_BAD_HTTP)
    assert not valid_mdns_service_name(_BAD_ESPHOME)
    assert not valid_mdns_service_name(f"{'x' * 64}.{_HTTP}")


@pytest.mark.parametrize("state_change", _ALL_CHANGES)
@pytest.mark.parametrize(("service_type", "name"), [(_HTTP, _BAD_HTTP), (_ESPHOME, _BAD_ESPHOME)])
def test_browser_dispatch_drops_malformed_name(
    service_type: str, name: str, state_change: ServiceStateChange
) -> None:
    """The #2620 crash path: a raw wire name zeroconf's ServiceInfo rejects."""
    monitor = _make_monitor()

    monitor.mdns._on_browser_event(MagicMock(), service_type, name, state_change)

    assert not monitor._tasks
    assert not monitor.state.http_urls
    assert not monitor.importable._import_discovery.import_state


def test_browser_dispatch_guard_precedes_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monitor = _make_monitor()
    calls: list[str] = []
    monkeypatch.setattr(
        monitor.mdns,
        "_on_http_service_state_change",
        lambda *_a: calls.append("monitor"),
    )
    monkeypatch.setattr(
        monitor.importable,
        "on_http_service_state_change",
        lambda *_a: calls.append("importable"),
    )

    monitor.mdns._on_browser_event(MagicMock(), _HTTP, _BAD_HTTP, ServiceStateChange.Added)
    assert calls == []

    monitor.mdns._on_browser_event(MagicMock(), _HTTP, f"klo.{_HTTP}", ServiceStateChange.Added)
    assert calls == ["monitor", "importable"]
