"""Tests for the mDNS responder's interface and IP version selection."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from zeroconf import InterfaceChoice, IPVersion

import esphome_device_builder.controllers._device_state_monitor.interface_monitor as im
from esphome_device_builder.controllers._device_state_monitor import mdns as mdns_module
from esphome_device_builder.controllers._device_state_monitor.interface_monitor import (
    ZeroconfBinding,
)

from .conftest import make_state_monitor_with_callbacks

_LOOPBACK_V4 = SimpleNamespace(ip="127.0.0.1")
_LOOPBACK_V6 = SimpleNamespace(ip=("::1", 0, 0))
_LAN_V4 = SimpleNamespace(ip="192.168.1.2")
_LINK_LOCAL_V6 = SimpleNamespace(ip=("fe80::1", 0, 7))
_GLOBAL_V6 = SimpleNamespace(ip=("2001:db8::1", 0, 0))

_V4 = "192.168.1.2"
_V6 = "fe80::1%7"
_ALL = InterfaceChoice.All


@pytest.mark.parametrize(
    ("platform", "ips", "pinned", "expected"),
    [
        ("linux", [_LAN_V4, _LINK_LOCAL_V6], None, ([_V4, _V6], IPVersion.All)),
        (
            "linux",
            [_LOOPBACK_V4, _LAN_V4, _GLOBAL_V6, _LINK_LOCAL_V6],
            None,
            ([_V4, _V6], IPVersion.All),
        ),
        ("linux", [_LAN_V4, _LAN_V4], None, ([_V4], IPVersion.V4Only)),
        ("linux", [_LAN_V4, _LOOPBACK_V6], None, ([_V4], IPVersion.V4Only)),
        ("linux", [_LAN_V4, _GLOBAL_V6], None, ([_V4], IPVersion.V4Only)),
        ("linux", [_LOOPBACK_V4, _LOOPBACK_V6], None, (_ALL, IPVersion.V4Only)),
        ("linux", [_LAN_V4, _LINK_LOCAL_V6], IPVersion.V4Only, ([_V4], IPVersion.V4Only)),
        ("linux", [_LINK_LOCAL_V6], IPVersion.V4Only, (_ALL, IPVersion.V4Only)),
        ("win32", [_LAN_V4, _LINK_LOCAL_V6], None, ([_V4, _V6], IPVersion.All)),
        ("darwin", [_LAN_V4, _LINK_LOCAL_V6], None, ([_V4], IPVersion.V4Only)),
        ("darwin", [_LOOPBACK_V4, _LINK_LOCAL_V6], None, ([_V6], IPVersion.V6Only)),
        ("darwin", [_LOOPBACK_V4, _LOOPBACK_V6], None, (_ALL, IPVersion.V4Only)),
        ("freebsd14", [_LAN_V4, _LINK_LOCAL_V6], None, ([_V4], IPVersion.V4Only)),
    ],
)
def test_zeroconf_binding(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    ips: list[Any],
    pinned: IPVersion | None,
    expected: tuple[Any, IPVersion],
) -> None:
    """The binding holds non-loopback IPv4 and link-local IPv6 for the platform's IP version."""
    monkeypatch.setattr(im.sys, "platform", platform)
    monkeypatch.setattr(im.ifaddr, "get_adapters", lambda: [SimpleNamespace(ips=ips)])

    assert im.zeroconf_binding(pinned) == expected


@pytest.mark.parametrize(
    ("interfaces", "expected"),
    [([_V4, _V6], [_V4]), ([_V6], _ALL), (_ALL, _ALL)],
)
def test_ipv4_only_binding(interfaces: Any, expected: Any) -> None:
    """Narrowing keeps the IPv4 addresses, or every interface when none remain."""
    binding = im.ipv4_only_binding(ZeroconfBinding(interfaces, IPVersion.All))

    assert binding == (expected, IPVersion.V4Only)


async def test_async_zeroconf_binding_scan_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed adapter scan resolves to every interface, IPv4 only."""
    monkeypatch.setattr(im.ifaddr, "get_adapters", MagicMock(side_effect=OSError))

    assert await im.async_zeroconf_binding() == (_ALL, IPVersion.V4Only)


def _patch_start(
    monkeypatch: pytest.MonkeyPatch, binding: ZeroconfBinding, failing: set[IPVersion]
) -> list[dict[str, Any]]:
    """Stub the binding scan and the responder; return the attempted constructor kwargs."""
    attempts: list[dict[str, Any]] = []

    def _fake_zeroconf(**kwargs: Any) -> MagicMock:
        attempts.append(kwargs)
        if kwargs["ip_version"] in failing:
            raise OSError("cannot bind")
        return MagicMock()

    monkeypatch.setattr(mdns_module, "async_zeroconf_binding", AsyncMock(return_value=binding))
    monkeypatch.setattr(mdns_module, "AsyncEsphomeZeroconf", _fake_zeroconf)
    monkeypatch.setattr(mdns_module, "AsyncServiceBrowser", MagicMock())
    monkeypatch.setattr(mdns_module, "monitor_interfaces", AsyncMock())
    return attempts


_DUAL_KWARGS = {"interfaces": [_V4, _V6], "ip_version": IPVersion.All}
_V4_KWARGS = {"interfaces": [_V4], "ip_version": IPVersion.V4Only}


@pytest.mark.parametrize(
    ("binding", "failing", "attempts", "started", "pinned"),
    [
        (ZeroconfBinding([_V4, _V6], IPVersion.All), set(), [_DUAL_KWARGS], True, None),
        (ZeroconfBinding([_V4], IPVersion.V4Only), set(), [_V4_KWARGS], True, None),
        (
            ZeroconfBinding([_V4, _V6], IPVersion.All),
            {IPVersion.All},
            [_DUAL_KWARGS, _V4_KWARGS],
            True,
            IPVersion.V4Only,
        ),
        (ZeroconfBinding([_V4], IPVersion.V4Only), {IPVersion.V4Only}, [_V4_KWARGS], False, None),
    ],
)
async def test_start_binding(
    monkeypatch: pytest.MonkeyPatch,
    binding: ZeroconfBinding,
    failing: set[IPVersion],
    attempts: list[dict[str, Any]],
    started: bool,
    pinned: IPVersion | None,
) -> None:
    """A failed non-IPv4 bind retries IPv4 only and pins the interface monitor to it."""
    tried = _patch_start(monkeypatch, binding, failing)
    monitor, _callbacks = make_state_monitor_with_callbacks([])

    await monitor.mdns.start()

    assert tried == attempts
    assert (monitor.mdns.zeroconf is not None) is started
    if started:
        mdns_module.monitor_interfaces.assert_called_once_with(monitor.mdns.zeroconf, pinned)
