"""Tests for the mDNS responder's IP version selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

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
        ("linux", [_LINK_LOCAL_V6], IPVersion.All),
        ("linux", [_LAN_V4], IPVersion.V4Only),
        ("linux", [_LAN_V4, _LOOPBACK_V6], IPVersion.V4Only),
        ("linux", [], IPVersion.V4Only),
        ("win32", [_LAN_V4, _LINK_LOCAL_V6], IPVersion.All),
        ("darwin", [_LAN_V4, _LINK_LOCAL_V6], IPVersion.V4Only),
        ("darwin", [_LOOPBACK_V4, _LINK_LOCAL_V6], IPVersion.V6Only),
        ("darwin", [_LOOPBACK_V4, _LOOPBACK_V6], IPVersion.V4Only),
        ("freebsd14", [_LAN_V4, _LINK_LOCAL_V6], IPVersion.V4Only),
        ("freebsd14", [_LINK_LOCAL_V6], IPVersion.V6Only),
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

    def _boom() -> None:
        raise OSError("adapters unavailable")

    monkeypatch.setattr(im.ifaddr, "get_adapters", _boom)

    assert await im.async_zeroconf_ip_version() is IPVersion.V4Only


def _patch_start(
    monkeypatch: pytest.MonkeyPatch, ip_version: IPVersion, failing: set[IPVersion]
) -> list[IPVersion]:
    """Stub the version scan and the responder; return the attempted versions."""
    attempts: list[IPVersion] = []

    async def _version() -> IPVersion:
        return ip_version

    def _fake_zeroconf(**kwargs: Any) -> MagicMock:
        attempts.append(kwargs["ip_version"])
        if kwargs["ip_version"] in failing:
            raise OSError("cannot bind")
        return MagicMock()

    async def _no_monitor(_zeroconf: Any) -> None:
        return None

    monkeypatch.setattr(mdns_module, "async_zeroconf_ip_version", _version)
    monkeypatch.setattr(mdns_module, "AsyncEsphomeZeroconf", _fake_zeroconf)
    monkeypatch.setattr(mdns_module, "AsyncServiceBrowser", MagicMock())
    monkeypatch.setattr(mdns_module, "monitor_interfaces", _no_monitor)
    return attempts


@pytest.mark.parametrize("ip_version", list(IPVersion))
async def test_start_uses_selected_ip_version(
    monkeypatch: pytest.MonkeyPatch, ip_version: IPVersion
) -> None:
    """The responder is created with the selected IP version."""
    attempts = _patch_start(monkeypatch, ip_version, failing=set())
    monitor, _callbacks = make_state_monitor_with_callbacks([])

    await monitor.mdns.start()

    assert attempts == [ip_version]
    assert monitor.mdns.zeroconf is not None


async def test_start_retries_ipv4_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed dual-stack bind retries IPv4 only."""
    attempts = _patch_start(monkeypatch, IPVersion.All, failing={IPVersion.All})
    monitor, _callbacks = make_state_monitor_with_callbacks([])

    await monitor.mdns.start()

    assert attempts == [IPVersion.All, IPVersion.V4Only]
    assert monitor.mdns.zeroconf is not None


async def test_start_ipv4_only_failure_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed IPv4-only bind leaves the responder unset after one attempt."""
    attempts = _patch_start(monkeypatch, IPVersion.V4Only, failing={IPVersion.V4Only})
    monitor, _callbacks = make_state_monitor_with_callbacks([])

    await monitor.mdns.start()

    assert attempts == [IPVersion.V4Only]
    assert monitor.mdns.zeroconf is None
