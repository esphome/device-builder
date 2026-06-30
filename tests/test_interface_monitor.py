"""Tests for the zeroconf interface-change poller.

``monitor_interfaces`` snapshots the host's addresses on a timer and calls
``async_update_interfaces`` only when they change; ``MdnsSource`` owns the task
and tears it down before closing zeroconf.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import esphome_device_builder.controllers._device_state_monitor.interface_monitor as im
from esphome_device_builder.controllers._device_state_monitor.interface_monitor import (
    monitor_interfaces,
)

_A = frozenset({("10.0.0.5", 24)})
_B = frozenset({("10.0.0.5", 24), ("192.168.1.2", 24)})

# Sentinel a scripted snapshot yields to make ``address_snapshot`` raise that tick.
_RAISE = object()


def _snapshots(monkeypatch: pytest.MonkeyPatch, values: list[frozenset[tuple[str, int]]]) -> None:
    """Feed ``address_snapshot`` a scripted sequence; the last value repeats."""
    seq = iter(values)
    last = values[-1]

    def _next() -> frozenset[tuple[str, int]]:
        nonlocal last
        last = next(seq, last)
        return last

    monkeypatch.setattr(im, "address_snapshot", _next)


async def _run_ticks(zeroconf: Any, ticks: int) -> None:
    """Run ``monitor_interfaces`` for *ticks* sleeps, then cancel cleanly."""
    seen = 0
    real_sleep = asyncio.sleep

    async def _counting_sleep(_interval: float) -> None:
        nonlocal seen
        seen += 1
        if seen >= ticks:
            raise asyncio.CancelledError
        await real_sleep(0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(im.asyncio, "sleep", _counting_sleep)
        with pytest.raises(asyncio.CancelledError):
            await monitor_interfaces(zeroconf, interval=0)


async def test_reconciles_when_addresses_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """A change between ticks triggers exactly one ``async_update_interfaces``."""
    # previous=_A (pre-loop), tick1 sees _A (no-op), tick2 sees _B (reconcile).
    _snapshots(monkeypatch, [_A, _A, _B])
    zeroconf = MagicMock()
    zeroconf.async_update_interfaces = AsyncMock()

    await _run_ticks(zeroconf, ticks=3)

    zeroconf.async_update_interfaces.assert_awaited_once()


async def test_no_op_when_addresses_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A constant snapshot never reconciles."""
    _snapshots(monkeypatch, [_A])
    zeroconf = MagicMock()
    zeroconf.async_update_interfaces = AsyncMock()

    await _run_ticks(zeroconf, ticks=3)

    zeroconf.async_update_interfaces.assert_not_awaited()


async def test_survives_reconcile_failure_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reconcile raise is swallowed; the change re-attempts on the next tick.

    ``previous`` is left unadvanced after a failure, so the still-different
    snapshot drives a second ``async_update_interfaces`` rather than the loop
    dying or the change being lost.
    """
    # previous=_A; both ticks see _B → reconcile attempted twice (1st raises).
    _snapshots(monkeypatch, [_A, _B, _B])
    zeroconf = MagicMock()
    zeroconf.async_update_interfaces = AsyncMock(side_effect=[RuntimeError("flap"), None])

    await _run_ticks(zeroconf, ticks=3)

    assert zeroconf.async_update_interfaces.await_count == 2


async def test_advances_previous_after_successful_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once reconciled, the same address set doesn't reconcile again."""
    # previous=_A; tick1 _B (reconcile), tick2 _B (now equals previous → no-op).
    _snapshots(monkeypatch, [_A, _B, _B])
    zeroconf = MagicMock()
    zeroconf.async_update_interfaces = AsyncMock()

    await _run_ticks(zeroconf, ticks=3)

    zeroconf.async_update_interfaces.assert_awaited_once()


async def test_snapshot_is_hashable_and_order_independent() -> None:
    """``address_snapshot`` returns a frozenset so equality ignores adapter order."""
    snap = im.address_snapshot()
    assert isinstance(snap, frozenset)
    # Reversing the underlying iteration order must not change equality.
    assert frozenset(reversed(list(snap))) == snap


async def test_snapshot_failure_does_not_kill_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising ``address_snapshot`` is swallowed; the loop keeps polling and reconciles later.

    A transient ``ifaddr`` error on one tick must not terminate the reconciler
    for the rest of the process; the next good snapshot still drives a change.
    """
    # previous=_A; tick1 snapshot raises (skipped), tick2 sees _B → reconcile.
    seq = iter([_A, _RAISE, _B, _B])

    def _next() -> frozenset[tuple[str, int]]:
        value = next(seq, _B)
        if value is _RAISE:
            raise OSError("adapters momentarily unavailable")
        return value  # type: ignore[return-value]

    monkeypatch.setattr(im, "address_snapshot", _next)
    zeroconf = MagicMock()
    zeroconf.async_update_interfaces = AsyncMock()

    await _run_ticks(zeroconf, ticks=3)

    zeroconf.async_update_interfaces.assert_awaited_once()


def test_address_snapshot_normalizes_ipv4_and_ipv6_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """v4 stays a plain string; link-local v6 keeps ``%scope`` and drops flowinfo; no tuple repr."""
    v4 = SimpleNamespace(ip="10.0.0.5", network_prefix=24)
    # ifaddr renders v6 as ``(addr, flowinfo, scope_id)``.
    v6_link_local = SimpleNamespace(ip=("fe80::1", 0, 7), network_prefix=64)
    v6_global = SimpleNamespace(ip=("2001:db8::1", 0, 0), network_prefix=64)
    adapter = SimpleNamespace(ips=[v4, v6_link_local, v6_global])
    monkeypatch.setattr(im.ifaddr, "get_adapters", lambda: [adapter])

    snap = im.address_snapshot()

    assert ("10.0.0.5", 24) in snap
    assert ("fe80::1%7", 64) in snap  # scope kept, flowinfo dropped
    assert ("2001:db8::1", 64) in snap  # scope 0 → no suffix
    # No raw ``(addr, flowinfo, scope)`` tuple leaked into the snapshot.
    assert all(isinstance(addr, str) and "(" not in addr for addr, _prefix in snap)
