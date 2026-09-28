"""Tests for the zeroconf interface-change poller.

``monitor_interfaces`` scans the host's addresses on a timer and calls
``async_update_interfaces`` when the responder's binding changes; ``MdnsSource``
owns the task and tears it down before closing zeroconf.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from zeroconf import InterfaceChoice, IPVersion

import esphome_device_builder.controllers._device_state_monitor.interface_monitor as im
from esphome_device_builder.controllers._device_state_monitor.interface_monitor import (
    FALLBACK_BINDING,
    HostAddresses,
    ZeroconfBinding,
    monitor_interfaces,
)

_V4 = "10.0.0.5"
_V6 = "fe80::1%7"

_HOST_V4 = HostAddresses((_V4,), ())
_HOST_DUAL = HostAddresses((_V4,), (_V6,))

_BOUND_V4 = ZeroconfBinding([_V4], IPVersion.V4Only)
_BOUND_DUAL = ZeroconfBinding([_V4, _V6], IPVersion.All)

# Sentinel a scripted scan yields to make ``scan_host`` raise that tick.
_RAISE = object()


@pytest.fixture(autouse=True)
def _linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Select IP versions as a dual-stack-capable platform."""
    monkeypatch.setattr(im.sys, "platform", "linux")


def _scans(monkeypatch: pytest.MonkeyPatch, values: list[Any]) -> None:
    """Feed ``scan_host`` a scripted sequence; the last value repeats."""
    seq = iter(values)
    last = values[-1]

    def _next() -> HostAddresses:
        nonlocal last
        last = next(seq, last)
        if last is _RAISE:
            raise OSError("adapters momentarily unavailable")
        return last

    monkeypatch.setattr(im, "scan_host", _next)


def _zeroconf(side_effect: list[Any] | None = None) -> MagicMock:
    zeroconf = MagicMock()
    zeroconf.async_update_interfaces = AsyncMock(side_effect=side_effect)
    return zeroconf


def _reconciled(zeroconf: MagicMock) -> list[ZeroconfBinding]:
    """Return the bindings *zeroconf* was reconciled with, in order."""
    return [
        ZeroconfBinding(**call.kwargs) for call in zeroconf.async_update_interfaces.await_args_list
    ]


async def _run_ticks(
    zeroconf: Any,
    ticks: int,
    applied: ZeroconfBinding | None = _BOUND_V4,
    pinned_ip_version: IPVersion | None = None,
    interval: float = 0,
) -> list[float]:
    """Run ``monitor_interfaces`` for *ticks* sleeps, then cancel; return the sleep delays."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _counting_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= ticks:
            raise asyncio.CancelledError
        await real_sleep(0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(im.asyncio, "sleep", _counting_sleep)
        with pytest.raises(asyncio.CancelledError):
            await monitor_interfaces(zeroconf, applied, pinned_ip_version, interval)
    return delays


async def test_reconciles_when_binding_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binding change between ticks reconciles with the new binding."""
    # tick1 settles on _HOST_V4, tick2 sees it again (no-op), tick3 sees _HOST_DUAL.
    _scans(monkeypatch, [_HOST_V4, _HOST_V4, _HOST_DUAL])
    zeroconf = _zeroconf()

    await _run_ticks(zeroconf, ticks=4)

    assert _reconciled(zeroconf) == [_BOUND_V4, _BOUND_DUAL]


async def test_unchanged_binding_reconciles_once_to_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constant binding reconciles on the settle tick only."""
    _scans(monkeypatch, [_HOST_V4])
    zeroconf = _zeroconf()

    await _run_ticks(zeroconf, ticks=4)

    assert _reconciled(zeroconf) == [_BOUND_V4]


async def test_change_arms_one_settle_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    """A binding change is followed by exactly one repeat reconcile."""
    _scans(monkeypatch, [_HOST_V4, _HOST_DUAL])
    zeroconf = _zeroconf()

    await _run_ticks(zeroconf, ticks=6)

    assert _reconciled(zeroconf) == [_BOUND_V4, _BOUND_DUAL, _BOUND_DUAL]


async def test_settle_ticks_use_the_short_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ticks after startup and after a change sleep ``_SETTLE_DELAY``; the rest the interval."""
    _scans(monkeypatch, [_HOST_V4, _HOST_V4, _HOST_DUAL])
    settle = im._SETTLE_DELAY

    delays = await _run_ticks(_zeroconf(), ticks=6, interval=300)

    assert delays == [settle, 300, 300, settle, 300, 300]


async def test_survives_reconcile_failure_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reconcile raise is swallowed; the change re-attempts on the next tick.

    ``applied`` is left unadvanced after a failure, so the still-different
    binding drives a second ``async_update_interfaces`` rather than the loop
    dying or the change being lost.
    """
    _scans(monkeypatch, [_HOST_DUAL])
    zeroconf = _zeroconf([RuntimeError("flap"), None, None])

    await _run_ticks(zeroconf, ticks=3)

    assert _reconciled(zeroconf) == [_BOUND_DUAL, _BOUND_DUAL]


async def test_scan_failure_does_not_kill_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising ``scan_host`` is swallowed; the loop keeps polling and reconciles later.

    A transient ``ifaddr`` error on one tick must not terminate the reconciler
    for the rest of the process; the next good scan still drives a change.
    """
    # tick1 scan raises (skipped), tick2 sees _HOST_DUAL → reconcile.
    _scans(monkeypatch, [_RAISE, _HOST_DUAL])
    zeroconf = _zeroconf()

    await _run_ticks(zeroconf, ticks=3)

    assert _reconciled(zeroconf) == [_BOUND_DUAL]


async def test_pinned_ip_version_narrows_the_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned IP version keeps a dual-stack host on its IPv4 addresses."""
    _scans(monkeypatch, [_HOST_DUAL])
    zeroconf = _zeroconf()

    await _run_ticks(zeroconf, ticks=3, pinned_ip_version=IPVersion.V4Only)

    assert _reconciled(zeroconf) == [_BOUND_V4]


async def test_fallback_binding_is_replaced_on_first_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """A responder bound from a failed startup scan reconciles to the scanned binding."""
    _scans(monkeypatch, [_HOST_DUAL])
    zeroconf = _zeroconf()

    await _run_ticks(zeroconf, ticks=2, applied=FALLBACK_BINDING)

    assert _reconciled(zeroconf) == [_BOUND_DUAL]


def test_scan_host_normalizes_and_filters_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """v4 stays a plain string; link-local v6 keeps ``%scope``; loopback and routable v6 drop."""
    ips = [
        SimpleNamespace(ip="127.0.0.1"),
        SimpleNamespace(ip=_V4),
        SimpleNamespace(ip=_V4),
        # ifaddr renders v6 as ``(addr, flowinfo, scope_id)``.
        SimpleNamespace(ip=("::1", 0, 0)),
        SimpleNamespace(ip=("fe80::1", 0, 7)),
        SimpleNamespace(ip=("2001:db8::1", 0, 0)),
        SimpleNamespace(ip=("fd00::1", 0, 0)),
    ]
    monkeypatch.setattr(im.ifaddr, "get_adapters", lambda: [SimpleNamespace(ips=ips)])

    assert im.scan_host() == ((_V4,), (_V6,))


@pytest.mark.parametrize(
    ("platform", "addresses", "pinned", "expected"),
    [
        ("linux", _HOST_DUAL, None, _BOUND_DUAL),
        ("linux", _HOST_V4, None, _BOUND_V4),
        ("linux", HostAddresses((), (_V6,)), None, ([_V6], IPVersion.All)),
        ("linux", HostAddresses((), ()), None, FALLBACK_BINDING),
        ("linux", _HOST_DUAL, IPVersion.V4Only, _BOUND_V4),
        ("linux", HostAddresses((), (_V6,)), IPVersion.V4Only, FALLBACK_BINDING),
        ("win32", _HOST_DUAL, None, _BOUND_DUAL),
        ("darwin", _HOST_DUAL, None, _BOUND_V4),
        ("darwin", HostAddresses((), (_V6,)), None, ([_V6], IPVersion.V6Only)),
        ("darwin", HostAddresses((), ()), None, FALLBACK_BINDING),
        ("freebsd14", _HOST_DUAL, None, _BOUND_V4),
    ],
)
def test_binding(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    addresses: HostAddresses,
    pinned: IPVersion | None,
    expected: tuple[Any, IPVersion],
) -> None:
    """The binding holds the addresses of the platform's IP version, or every interface."""
    monkeypatch.setattr(im.sys, "platform", platform)

    assert addresses.binding(pinned) == expected


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        (None, [FALLBACK_BINDING]),
        (_HOST_V4, [_BOUND_V4]),
        (_HOST_DUAL, [_BOUND_DUAL, _BOUND_V4]),
        (
            HostAddresses((), (_V6,)),
            [([_V6], IPVersion.All), (InterfaceChoice.All, IPVersion.V4Only)],
        ),
    ],
)
def test_startup_bindings(addresses: HostAddresses | None, expected: list[Any]) -> None:
    """Startup tries the selected binding, then its IPv4-only fallback when it differs."""
    assert im.startup_bindings(addresses) == expected


async def test_async_scan_host_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed adapter scan resolves to ``None``."""
    monkeypatch.setattr(im.ifaddr, "get_adapters", MagicMock(side_effect=OSError))

    assert await im.async_scan_host() is None
