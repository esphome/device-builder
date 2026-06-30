"""Tests for the zeroconf interface-change poller.

``monitor_interfaces`` snapshots the host's addresses on a timer and calls
``async_update_interfaces`` only when they change; ``MdnsSource`` owns the task
and tears it down before closing zeroconf.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import esphome_device_builder.controllers._device_state_monitor.interface_monitor as im
from esphome_device_builder.controllers._device_state_monitor.interface_monitor import (
    monitor_interfaces,
)

_A = frozenset({("10.0.0.5", 24)})
_B = frozenset({("10.0.0.5", 24), ("192.168.1.2", 24)})


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
