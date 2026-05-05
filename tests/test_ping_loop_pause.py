"""
Strict-pause behaviour for the ICMP ping loop.

Mirrors the legacy ``esphome.dashboard.status.ping`` /
``web_server.py`` pair where ICMP only fired while
``self._subscribers`` was non-empty (the new dashboard had been
sweeping unconditionally — bug). Pins:

* with no presence gate, the loop runs as before (legacy /
  unit-test parity)
* with a wired ``SubscriberPresence``, ``_ping_loop`` parks
  before the sweep until the first subscriber arrives
* the 0→1 subscriber transition wakes the loop within one
  scheduling tick — no waiting for ``_PING_INTERVAL``
* the 1→0 transition lets the next iteration park again so a
  burst of disconnects doesn't keep ICMP looping
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder import device_builder as device_builder_module
from esphome_device_builder.controllers import _device_state_monitor as state_monitor_module
from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.subscriber_presence import SubscriberPresence


def _build_monitor(presence: SubscriberPresence | None) -> DeviceStateMonitor:
    """Bypass __init__ — ``_ping_loop`` only touches a few attrs."""
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)
    monitor._presence = presence
    return monitor


def _instrument_loop(
    monitor: DeviceStateMonitor, monkeypatch: pytest.MonkeyPatch
) -> dict[str, int]:
    """Replace the work the loop does each tick with call counters.

    Lets the test assert "swept N times" without needing the real
    DNS cache, zeroconf instance, or ICMP primitive. Returns the
    counter dict so each test can read ``counts["sweeps"]`` after
    driving the loop.
    """
    counts = {"sweeps": 0, "resolves": 0, "sleeps": 0}

    async def _resolve() -> None:
        counts["resolves"] += 1

    async def _sweep() -> None:
        counts["sweeps"] += 1

    monitor._resolve_non_api_mdns_targets = _resolve  # type: ignore[method-assign]
    monitor._ping_sweep = _sweep  # type: ignore[method-assign]

    # Skip the bootstrap delay outright.
    monkeypatch.setattr(state_monitor_module, "_PING_BOOTSTRAP_DELAY", 0)

    # Patch the module-local sleep so each "interval wait" is a
    # zero-cost yield and a tick count. The test ends the loop by
    # cancelling the task, not by raising CancelledError from sleep.
    real_sleep = asyncio.sleep

    async def _fast_sleep(_seconds: float) -> None:
        counts["sleeps"] += 1
        # Yield back so the test coroutine can observe the counters.
        await real_sleep(0)

    monkeypatch.setattr(state_monitor_module.asyncio, "sleep", _fast_sleep)
    return counts


async def _drive_until(condition: Any, *, timeout: float = 0.5) -> None:
    """Wait for *condition()* to become truthy or raise on timeout."""

    async def _spin() -> None:
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(_spin(), timeout=timeout)


@pytest.mark.asyncio
async def test_ping_loop_runs_unconditionally_without_presence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``presence=None`` (legacy / unit-test default) keeps ICMP looping.

    Pin the back-compat path: tests that build a monitor without
    wiring a presence gate must still see the ping pipeline run
    every tick, otherwise the existing ping-loop test suite would
    silently park forever.
    """
    monitor = _build_monitor(presence=None)
    counts = _instrument_loop(monitor, monkeypatch)

    task = asyncio.create_task(monitor._ping_loop())
    try:
        await _drive_until(lambda: counts["sweeps"] >= 2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert counts["sweeps"] >= 2
    assert counts["resolves"] >= 2


@pytest.mark.asyncio
async def test_ping_loop_parks_until_first_subscriber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a presence gate, no sweep happens until someone subscribes.

    Closes the legacy-parity regression: the new dashboard had been
    pinging the fleet every minute regardless of whether a UI was
    listening. The loop must reach ``_ping_sweep`` only after the
    0→1 subscriber transition.
    """
    presence = SubscriberPresence()
    monitor = _build_monitor(presence=presence)
    counts = _instrument_loop(monitor, monkeypatch)

    task = asyncio.create_task(monitor._ping_loop())
    try:
        # Give the loop several scheduling ticks to confirm it
        # actually parks instead of running. Without the gate fix
        # ``_ping_sweep`` would have fired on the first tick.
        for _ in range(20):
            await asyncio.sleep(0)
        assert counts["sweeps"] == 0, "ping loop must not sweep while no subscriber is registered"

        # 0→1 transition must wake the loop within one scheduling tick.
        with presence.subscriber():
            await _drive_until(lambda: counts["sweeps"] >= 1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert counts["sweeps"] >= 1


@pytest.mark.asyncio
async def test_ping_loop_pauses_again_after_last_subscriber_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1→0 transition closes the gate so the next iteration parks.

    Without the re-arm, a subscriber that briefly connected once
    would keep the ICMP loop running forever afterwards (the
    asyncio.Event would stay set). Pin: after the subscriber
    disconnects, the sweep count stops climbing.
    """
    presence = SubscriberPresence()
    monitor = _build_monitor(presence=presence)
    counts = _instrument_loop(monitor, monkeypatch)

    task = asyncio.create_task(monitor._ping_loop())
    try:
        # Cycle one subscriber in, drive at least one sweep, then out.
        with presence.subscriber():
            await _drive_until(lambda: counts["sweeps"] >= 1)
        sweeps_at_disconnect = counts["sweeps"]

        # After disconnect, give the loop several ticks. The count
        # should plateau at most one sweep above where it was — the
        # loop completes whatever sweep was already in flight, then
        # parks at the gate on the next iteration.
        for _ in range(20):
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # At most one extra sweep can land — the one already past the
    # gate when the subscriber dropped. Anything more means the gate
    # didn't close on 1→0.
    assert counts["sweeps"] <= sweeps_at_disconnect + 1


@pytest.mark.asyncio
async def test_subscribe_events_holds_presence_for_stream_lifetime() -> None:
    """``_cmd_subscribe_events`` increments presence for its body.

    End-to-end-ish check that the controller wraps its broadcast
    stream in ``presence.subscriber()`` so the gate's count
    actually moves when a real WS subscribe lands. We don't drive
    the full ``stream_events`` here — that's exercised in the
    subscribe_events tests; we just pin that the wrap is in place
    by stubbing ``stream_events`` and watching the counter while
    inside the stub.
    """
    builder = DeviceBuilder.__new__(DeviceBuilder)
    builder.bus = AsyncMock()
    builder.subscriber_presence = SubscriberPresence()
    builder.devices = None  # short-circuits _send_initial's branch

    counts: dict[str, int] = {"inside": 0, "outside_after": 0}

    async def _fake_stream_events(**_kwargs: Any) -> None:
        counts["inside"] = builder.subscriber_presence.count
        await asyncio.sleep(0)

    original = device_builder_module.stream_events
    device_builder_module.stream_events = _fake_stream_events  # type: ignore[assignment]
    try:
        client = AsyncMock()
        await builder._cmd_subscribe_events(client=client, message_id="m1")
    finally:
        device_builder_module.stream_events = original  # type: ignore[assignment]

    counts["outside_after"] = builder.subscriber_presence.count
    assert counts["inside"] == 1, "presence count must be 1 inside the stream body"
    assert counts["outside_after"] == 0, "presence count must drop back to 0 after stream exit"
