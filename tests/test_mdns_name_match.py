"""Regression tests for the mDNS service-name → device lookup.

mDNS broadcasts ``<device-name>._esphomelib._tcp.local.``; the
left-hand label is the device's ``esphome.name`` verbatim. Modern
configs use ``friendly_name_slugify``-style names with hyphens
(``apollo-r-pro-1-eth-5938e0``); the previous code converted those
hyphens to underscores before lookup, so every modern device's
mDNS announcement landed on a non-existent ``apollo_r_pro_...`` key
and the device stayed marked Unknown forever.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from zeroconf import ServiceStateChange

from esphome_device_builder.controllers import _device_state_monitor as monitor_module
from esphome_device_builder.controllers._device_state_monitor import (
    DeviceStateMonitor,
    device_name_from_service,
)
from esphome_device_builder.models import Device, DeviceState


def _device(name: str) -> Device:
    return Device(
        name=name,
        friendly_name=name,
        configuration=f"{name}.yaml",
        address=f"{name}.local",
        state=DeviceState.UNKNOWN,
    )


# ----------------------------------------------------------------------
# device_name_from_service helper — the bit ``_on_service_state_change``
# actually uses to compute the catalog key.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "service_name,expected",
    [
        # Modern hyphenated device — the previously-failing case.
        ("apollo-r-pro-1-eth-5938e0._esphomelib._tcp.local.", "apollo-r-pro-1-eth-5938e0"),
        ("home-assistant-voice-090073._esphomelib._tcp.local.", "home-assistant-voice-090073"),
        # Underscored YAML name (older convention) — must still work.
        ("legacy_device._esphomelib._tcp.local.", "legacy_device"),
        # Single-word name — sanity check.
        ("steamreset._esphomelib._tcp.local.", "steamreset"),
    ],
)
def test_device_name_from_service_preserves_label(service_name: str, expected: str) -> None:
    """The label is returned verbatim — no hyphen↔underscore substitution."""
    assert device_name_from_service(service_name) == expected


# ----------------------------------------------------------------------
# _on_service_state_change end-to-end (stubbed browser)
# ----------------------------------------------------------------------


class _FakeServiceInfo:
    """Stand-in for ``AsyncServiceInfo`` whose cache always hits.

    Lets us drive ``_on_service_state_change``'s synchronous path
    without booting real zeroconf — the handler calls
    ``info.load_from_cache(zeroconf)`` first and only spawns a
    network-resolve task on miss.
    """

    def __init__(self, _service_type: str, _name: str) -> None:
        pass

    def load_from_cache(self, _zc: Any) -> bool:
        return True

    def parsed_addresses(self, _ip_version: Any) -> list[str]:
        return []

    @property
    def decoded_properties(self) -> dict[str, str | None]:
        return {}


async def _capture_handler(monitor: DeviceStateMonitor, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Boot the mDNS browser with stubs, return the inner handler."""
    captured: dict[str, Any] = {}

    class _FakeBrowser:
        def __init__(self, _zc: Any, _service_type: str, *, handlers: list[Any]) -> None:
            captured["handler"] = handlers[0]

    fake_zc = MagicMock()
    monkeypatch.setattr(monitor_module, "AsyncEsphomeZeroconf", lambda: fake_zc)
    monkeypatch.setattr(monitor_module, "AsyncServiceInfo", _FakeServiceInfo)
    monkeypatch.setattr("zeroconf.asyncio.AsyncServiceBrowser", _FakeBrowser)

    await monitor._start_mdns_browser()
    return captured["handler"]


async def test_handler_marks_hyphenated_device_online(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hyphenated mDNS announcement marks the matching catalog entry online.

    Pre-fix the handler did ``.replace("-", "_")`` before lookup, so
    ``apollo-r-pro-1-eth-5938e0`` matched nothing in the catalog and
    the device stayed Unknown until the 60s ping sweep.
    """
    devices = [_device("apollo-r-pro-1-eth-5938e0")]
    on_state = MagicMock()
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=on_state,
        on_ip_change=MagicMock(),
        on_version_change=MagicMock(),
    )

    handler = await _capture_handler(monitor, monkeypatch)

    handler(
        MagicMock(),
        "_esphomelib._tcp.local.",
        "apollo-r-pro-1-eth-5938e0._esphomelib._tcp.local.",
        ServiceStateChange.Added,
    )

    on_state.assert_any_call("apollo-r-pro-1-eth-5938e0", DeviceState.ONLINE, "mdns")


async def test_handler_does_not_substitute_hyphens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hyphenated YAML must not be looked up via underscores.

    Catches a regression that re-introduces the hyphen substitution:
    a device named ``my-device`` would then never see its mDNS
    announcement reach the catalog if the handler turned the label
    into ``my_device`` before lookup.
    """
    devices = [_device("my-device")]
    on_state = MagicMock()
    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=on_state,
        on_ip_change=MagicMock(),
        on_version_change=MagicMock(),
    )

    handler = await _capture_handler(monitor, monkeypatch)
    handler(
        MagicMock(),
        "_esphomelib._tcp.local.",
        "my-device._esphomelib._tcp.local.",
        ServiceStateChange.Added,
    )

    on_state.assert_called_once_with("my-device", DeviceState.ONLINE, "mdns")
