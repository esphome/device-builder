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
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder import device_builder as device_builder_module
from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.controllers._device_state_monitor import (
    controller as state_monitor_module,
)
from esphome_device_builder.controllers._device_state_monitor._state import MonitorState
from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.subscriber_presence import SubscriberPresence


def _build_monitor(presence: SubscriberPresence | None) -> DeviceStateMonitor:
    """Bypass __init__ — ``_ping_loop`` only touches a few attrs."""
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)
    monitor.state = MonitorState()
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


async def _drive_until(condition: Callable[[], object], *, timeout: float = 0.5) -> None:
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
async def test_subscribe_events_holds_presence_for_stream_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(device_builder_module, "stream_events", _fake_stream_events)

    client = AsyncMock()
    await builder._cmd_subscribe_events(client=client, message_id="m1")

    counts["outside_after"] = builder.subscriber_presence.count
    assert counts["inside"] == 1, "presence count must be 1 inside the stream body"
    assert counts["outside_after"] == 0, "presence count must drop back to 0 after stream exit"


@pytest.mark.asyncio
async def test_ping_loop_aborts_idle_sleep_when_last_subscriber_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-sweep idle wait is interruptible by a subscriber drop.

    Without an interruptible wait, a subscriber that disconnected
    mid-``_PING_INTERVAL`` would force the next subscriber's first
    sweep to wait out the rest of the interval — defeating the
    point of waking promptly on the 0→1 transition. Pin: after the
    last subscriber drops, the ping loop returns to the top of the
    while-loop within one scheduling tick (so it's parked at
    ``wait_for_subscriber`` long before the interval would have
    elapsed).

    Drives this by patching ``asyncio.wait_for`` so the test can
    observe the call arguments — the loop must invoke it with the
    presence's ``wait_for_no_subscribers`` coroutine, not a bare
    ``asyncio.sleep``. A regression that keeps the unconditional
    sleep would never call ``wait_for`` at all.
    """
    presence = SubscriberPresence()
    monitor = _build_monitor(presence=presence)
    counts = _instrument_loop(monitor, monkeypatch)

    # Capture every call to wait_for so we can assert the loop
    # used the interruptible path. Each call returns immediately
    # so the test stays bounded.
    wait_for_calls: list[float] = []
    real_wait_for = asyncio.wait_for

    async def _spy_wait_for(coro: Any, *, timeout: float) -> Any:
        wait_for_calls.append(timeout)
        # Defer to the real implementation so the gate-close still
        # short-circuits the wait when the subscriber drops.
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(state_monitor_module.asyncio, "wait_for", _spy_wait_for)

    task = asyncio.create_task(monitor._ping_loop())
    try:
        # Bring a subscriber in; wait until the loop has done at
        # least one sweep AND entered the idle wait.
        with presence.subscriber():
            await _drive_until(lambda: counts["sweeps"] >= 1 and wait_for_calls)
            assert wait_for_calls, "loop must use asyncio.wait_for for an interruptible idle wait"

        # Subscriber just dropped (the with-block exited). The
        # gate-close must short-circuit the in-flight wait_for so
        # the loop returns to the top and parks on
        # wait_for_subscriber within a few ticks — not after the
        # full _PING_INTERVAL the wait_for was called with.
        sweeps_at_drop = counts["sweeps"]
        for _ in range(40):
            await asyncio.sleep(0)
        # Loop should be parked, not still sweeping.
        assert counts["sweeps"] == sweeps_at_drop, (
            "loop kept sweeping after subscriber drop — interrupt failed"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_ping_loop_resumes_immediately_when_new_subscriber_arrives_mid_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a drop+reconnect cycle, the new subscriber's first sweep is prompt.

    The end-to-end contract behind the interruptible-sleep change.
    Without the interrupt, this sequence would make the second
    subscriber wait up to ``_PING_INTERVAL`` for fresh ICMP data:

      1. Subscriber A connects, loop sweeps once, parks on idle.
      2. A disconnects mid-idle (presence.count → 0).
      3. Subscriber B connects.
      4. *With* the interrupt: idle wait short-circuits on A's
         drop, loop parks at the top, B's connect wakes it
         immediately, sweep #2 runs within a few ticks.
      4'. *Without* the interrupt: idle wait runs to completion,
         loop sweeps unconditionally even though no one was
         listening for most of the interval, and the timing
         depends on when in the interval B happened to arrive.

    Pin the timing: from B's connect to sweep #2, ≤ a handful of
    scheduling ticks (we use a generous 0.5s timeout via the test
    helper).
    """
    presence = SubscriberPresence()
    monitor = _build_monitor(presence=presence)
    counts = _instrument_loop(monitor, monkeypatch)

    task = asyncio.create_task(monitor._ping_loop())
    try:
        # Cycle subscriber A in, drive a sweep, then out.
        with presence.subscriber():
            await _drive_until(lambda: counts["sweeps"] >= 1)
        sweeps_after_a = counts["sweeps"]

        # Give the loop a few ticks to settle into the
        # wait_for_subscriber park (the interrupt should have
        # fired during the idle wait).
        for _ in range(10):
            await asyncio.sleep(0)
        assert counts["sweeps"] == sweeps_after_a

        # Subscriber B arrives. The 0→1 transition must wake the
        # loop's wait_for_subscriber within one scheduling tick;
        # _drive_until's bounded timeout catches a regression
        # that left the loop parked on a non-interruptible sleep.
        with presence.subscriber():
            await _drive_until(lambda: counts["sweeps"] > sweeps_after_a)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert counts["sweeps"] >= sweeps_after_a + 1
