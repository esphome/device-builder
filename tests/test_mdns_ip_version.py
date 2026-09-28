"""Tests for the mDNS responder's IP version selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from zeroconf import IPVersion

import esphome_device_builder.controllers._device_state_monitor.interface_monitor as im
from esphome_device_builder.controllers._device_state_monitor import mdns as mdns_module

from .conftest import make_state_monitor_with_callbacks

_LOOPBACK_V4 = SimpleNamespace(ip="127.0.0.1", is_IPv6=False)
_LOOPBACK_V6 = SimpleNamespace(ip=("::1", 0, 0), is_IPv6=True)
_LAN_V4 = SimpleNamespace(ip="192.168.1.2", is_IPv6=False)
_LINK_LOCAL_V6 = SimpleNamespace(ip=("fe80::1", 0, 7), is_IPv6=True)


@pytest.mark.parametrize(
    ("platform", "ips", "expected"),
    [
        ("linux", [_LAN_V4, _LINK_LOCAL_V6], IPVersion.All),
        ("linux", [_LAN_V4], IPVersion.V4Only),
        ("linux", [_LAN_V4, _LOOPBACK_V6], IPVersion.V4Only),
        ("linux", [], IPVersion.V4Only),
        ("win32", [_LAN_V4, _LINK_LOCAL_V6], IPVersion.All),
        ("darwin", [_LAN_V4, _LINK_LOCAL_V6], IPVersion.V4Only),
        ("darwin", [_LOOPBACK_V4, _LINK_LOCAL_V6], IPVersion.V6Only),
        ("darwin", [_LOOPBACK_V4, _LOOPBACK_V6], IPVersion.V4Only),
        ("freebsd14", [_LAN_V4, _LINK_LOCAL_V6], IPVersion.V4Only),
    ],
)
def test_zeroconf_ip_version(
    monkeypatch: pytest.MonkeyPatch, platform: str, ips: list[Any], expected: IPVersion
) -> None:
    """Dual-stack needs a non-loopback IPv6 address and a platform that supports it."""
    monkeypatch.setattr(im.sys, "platform", platform)
    monkeypatch.setattr(im.ifaddr, "get_adapters", lambda: [SimpleNamespace(ips=ips)])

    assert im.zeroconf_ip_version() is expected


async def test_async_zeroconf_ip_version_scan_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed adapter scan resolves to IPv4 only."""
    monkeypatch.setattr(im.ifaddr, "get_adapters", MagicMock(side_effect=OSError))

    assert await im.async_zeroconf_ip_version() is IPVersion.V4Only


def _patch_start(
    monkeypatch: pytest.MonkeyPatch, ip_version: IPVersion, failing: set[IPVersion]
) -> list[IPVersion]:
    """Stub the version scan and the responder; return the attempted versions."""
    attempts: list[IPVersion] = []

    def _fake_zeroconf(**kwargs: Any) -> MagicMock:
        attempts.append(kwargs["ip_version"])
        if kwargs["ip_version"] in failing:
            raise OSError("cannot bind")
        return MagicMock()

    monkeypatch.setattr(
        mdns_module, "async_zeroconf_ip_version", AsyncMock(return_value=ip_version)
    )
    monkeypatch.setattr(mdns_module, "AsyncEsphomeZeroconf", _fake_zeroconf)
    monkeypatch.setattr(mdns_module, "AsyncServiceBrowser", MagicMock())
    monkeypatch.setattr(mdns_module, "monitor_interfaces", AsyncMock())
    return attempts


@pytest.mark.parametrize(
    ("selected", "failing", "attempts", "started", "pinned"),
    [
        (IPVersion.All, set(), [IPVersion.All], True, None),
        (IPVersion.V4Only, set(), [IPVersion.V4Only], True, None),
        (IPVersion.All, {IPVersion.All}, [IPVersion.All, IPVersion.V4Only], True, IPVersion.V4Only),
        (IPVersion.V4Only, {IPVersion.V4Only}, [IPVersion.V4Only], False, None),
    ],
)
async def test_start_ip_version(
    monkeypatch: pytest.MonkeyPatch,
    selected: IPVersion,
    failing: set[IPVersion],
    attempts: list[IPVersion],
    started: bool,
    pinned: IPVersion | None,
) -> None:
    """A failed non-IPv4 bind retries IPv4 only and pins the interface monitor to it."""
    tried = _patch_start(monkeypatch, selected, failing)
    monitor, _callbacks = make_state_monitor_with_callbacks([])

    await monitor.mdns.start()

    assert tried == attempts
    assert (monitor.mdns.zeroconf is not None) is started
    if started:
        mdns_module.monitor_interfaces.assert_called_once_with(monitor.mdns.zeroconf, pinned)
