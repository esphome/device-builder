"""Tests for the mDNS responder's startup binding."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from zeroconf import IPVersion

import esphome_device_builder.controllers._device_state_monitor.interface_monitor as im
from esphome_device_builder.controllers._device_state_monitor import mdns as mdns_module
from esphome_device_builder.controllers._device_state_monitor.interface_monitor import (
    FALLBACK_BINDING,
    HostAddresses,
    ZeroconfBinding,
)

from .conftest import make_state_monitor_with_callbacks

_HOST_V4 = HostAddresses(("192.168.1.2",), ())
_HOST_DUAL = HostAddresses(("192.168.1.2",), ("fe80::1%7",))
_BOUND_V4 = ZeroconfBinding(["192.168.1.2"], IPVersion.V4Only)
_BOUND_DUAL = ZeroconfBinding(["192.168.1.2", "fe80::1%7"], IPVersion.All)


@pytest.mark.parametrize(
    ("addresses", "failing", "attempts", "pinned"),
    [
        (_HOST_DUAL, set(), [_BOUND_DUAL], None),
        (_HOST_DUAL, {IPVersion.All}, [_BOUND_DUAL, _BOUND_V4], IPVersion.V4Only),
        (_HOST_DUAL, {IPVersion.All, IPVersion.V4Only}, [_BOUND_DUAL, _BOUND_V4], None),
        (_HOST_V4, {IPVersion.V4Only}, [_BOUND_V4], None),
        (None, set(), [FALLBACK_BINDING], None),
    ],
)
async def test_start_binding(
    monkeypatch: pytest.MonkeyPatch,
    addresses: HostAddresses | None,
    failing: set[IPVersion],
    attempts: list[ZeroconfBinding],
    pinned: IPVersion | None,
) -> None:
    """A failed non-IPv4 bind retries IPv4 only and pins the interface monitor to it."""
    tried: list[ZeroconfBinding] = []

    def _fake_zeroconf(**kwargs: Any) -> MagicMock:
        tried.append(ZeroconfBinding(**kwargs))
        if kwargs["ip_version"] in failing:
            raise OSError("cannot bind")
        return MagicMock()

    monkeypatch.setattr(im.sys, "platform", "linux")
    monkeypatch.setattr(mdns_module, "async_scan_host", AsyncMock(return_value=addresses))
    monkeypatch.setattr(mdns_module, "AsyncEsphomeZeroconf", _fake_zeroconf)
    monkeypatch.setattr(mdns_module, "AsyncServiceBrowser", MagicMock())
    monitor_interfaces = AsyncMock()
    monkeypatch.setattr(mdns_module, "monitor_interfaces", monitor_interfaces)
    monitor, _callbacks = make_state_monitor_with_callbacks([])

    await monitor.mdns.start()

    assert tried == attempts
    if tried[-1].ip_version in failing:
        assert monitor.mdns.zeroconf is None
        monitor_interfaces.assert_not_called()
    else:
        monitor_interfaces.assert_called_once_with(monitor.mdns.zeroconf, tried[-1], pinned)
