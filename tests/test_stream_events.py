"""Tests for ``helpers.event_bus.stream_events``.

The helper lives next to ``EventBus`` because it owns three
correctness properties every WS-streaming command must get right:

1. **Snapshot+subscribe atomicity.** Listeners attach before
   ``send_initial`` is awaited.
2. **Bounded memory.** A slow follower can't accumulate every fired
   event in an unbounded queue.
3. **Cleanup on cancel.** Cancelling the helper task releases every
   listener.

Pinning these here means a refactor of the helper itself surfaces
in a focused test file rather than across every per-command suite
that uses it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from esphome_device_builder.helpers.event_bus import (
    _DEFAULT_STREAM_QUEUE_MAX,
    Event,
    EventBus,
    StreamBackpressureError,
    StreamControls,
    stream_events,
)
from esphome_device_builder.models import EventType


class _FakeClient:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, Any]] = []

    async def send_event(self, message_id: str, event: str, data: Any) -> None:
        # Yield so concurrent fires can interleave with the drain.
        await asyncio.sleep(0)
        self.events.append((message_id, event, data))


async def test_send_initial_runs_inside_listening_block() -> None:
    """Events fired during ``send_initial`` queue, don't drop.

    Closes the snapshot/live race: the helper must attach listeners
    *before* awaiting ``send_initial`` so an event firing during
    the seed-replay queues through the listener and lands strictly
    after the seed.
    """
    bus = EventBus()
    client = _FakeClient()

    async def _send_initial(_controls: StreamControls) -> None:
        # Fire a live event during the seed — this MUST queue, not
        # drop, because listeners are already attached.
        bus.fire(EventType.DEVICE_UPDATED, {"line": "during-seed"})
        await client.send_event("m1", "seed", "ok")

    def _handle_event(event: Event, controls: StreamControls) -> None:
        controls.push(event.event_type.value, event.data)
        # Once the live event is delivered we have what we need;
        # ending the stream lets the helper task return cleanly so
        # we can assert without racing cancellation.
        controls.end()

    await asyncio.wait_for(
        stream_events(
            client=client,
            message_id="m1",
            bus=bus,
            event_types=[EventType.DEVICE_UPDATED],
            handle_event=_handle_event,
            send_initial=_send_initial,
        ),
        timeout=2.0,
    )

    # The seed lands first, then the live event — strict ordering
    # is the contract this test pins.
    names = [(e, d) for (_m, e, d) in client.events]
    assert names == [
        ("seed", "ok"),
        ("device_updated", {"line": "during-seed"}),
    ]


async def test_cancellation_unsubscribes_every_listener() -> None:
    """Cancelling the helper task releases all listeners.

    Without this, every WS reconnect would leak ~one listener per
    subscribed ``EventType``. The closure keeps the closed client
    alive, so subsequent ``bus.fire`` calls iterate dead listeners
    forever.
    """
    bus = EventBus()
    client = _FakeClient()

    def _handle_event(_event: Event, _controls: StreamControls) -> None:
        pass

    task = asyncio.create_task(
        stream_events(
            client=client,
            message_id="m1",
            bus=bus,
            event_types=[EventType.DEVICE_UPDATED, EventType.DEVICE_REMOVED],
            handle_event=_handle_event,
        )
    )
    # Yield so the helper finishes its synchronous setup and parks
    # on ``queue.get``.
    await asyncio.sleep(0)

    listener_count_before = sum(len(bus._listeners.get(et, ())) for et in EventType)
    assert listener_count_before > 0, "no listeners attached"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    listener_count_after = sum(len(bus._listeners.get(et, ())) for et in EventType)
    assert listener_count_after == 0


async def test_end_breaks_drain_loop() -> None:
    """``controls.end()`` causes the helper to return cleanly.

    The terminal-job code path in ``follow_job`` relies on this:
    after the result event is pushed, ``end()`` is called so the
    helper exits instead of parking forever on ``queue.get``.
    """
    bus = EventBus()
    client = _FakeClient()

    def _handle_event(event: Event, controls: StreamControls) -> None:
        controls.push(event.event_type.value, event.data)
        # End the stream after the very first event.
        controls.end()

    task = asyncio.create_task(
        stream_events(
            client=client,
            message_id="m1",
            bus=bus,
            event_types=[EventType.DEVICE_UPDATED],
            handle_event=_handle_event,
        )
    )
    await asyncio.sleep(0)
    bus.fire(EventType.DEVICE_UPDATED, {"x": 1})

    await asyncio.wait_for(task, timeout=1.0)
    assert client.events == [("m1", "device_updated", {"x": 1})]


async def test_push_drops_newest_when_queue_full() -> None:
    """A slow follower's queue is bounded — newest line drops on full.

    Asserts strict equality on the delivered count: ``1`` (the
    parked first item the drain picked up before the gate
    closed) + ``_DEFAULT_STREAM_QUEUE_MAX`` (everything that fit
    in the bounded queue while the drain was blocked). With an
    unbounded queue the count would equal the full burst, so the
    strict equality below distinguishes bounded from unbounded
    cleanly.

    The earlier shape of this test cancelled the helper task
    immediately after releasing the drain gate, so cancellation
    raced ahead of the drain processing the backlog and the
    assertion ``len(received) <= 1 + cap`` passed even against
    an unbounded queue (because ``received`` never grew past
    the seed). Releasing the gate, yielding generously, and only
    *then* cancelling fixes that — the drain has time to flush
    every queued item before cancellation lands, so the
    delivered count actually reflects what the queue held.
    """
    bus = EventBus()

    drain_can_run = asyncio.Event()
    received: list[tuple[str, Any]] = []

    class GatedClient:
        async def send_event(self, _mid: str, event: str, data: Any) -> None:
            # First call (the seed item the drain picked up before
            # the backlog) parks until the test releases the gate;
            # subsequent calls run freely so the drain can consume
            # the backlog after the gate opens.
            received.append((event, data))
            if len(received) == 1:
                await drain_can_run.wait()

    def _handle_event(event: Event, controls: StreamControls) -> None:
        controls.push(event.event_type.value, event.data["i"])

    task = asyncio.create_task(
        stream_events(
            client=GatedClient(),
            message_id="m1",
            bus=bus,
            event_types=[EventType.DEVICE_UPDATED],
            handle_event=_handle_event,
        )
    )
    await asyncio.sleep(0)

    # First fire: drain consumes it, parks in send_event.
    bus.fire(EventType.DEVICE_UPDATED, {"i": 0})
    await asyncio.sleep(0)
    assert received == [("device_updated", 0)]

    # Fire well past the cap. With drain blocked, the queue caps at
    # maxsize and excess fires no-op via suppress(QueueFull).
    burst = _DEFAULT_STREAM_QUEUE_MAX + 500
    for i in range(1, burst + 1):
        bus.fire(EventType.DEVICE_UPDATED, {"i": i})

    # Release the gate and yield generously so the drain flushes
    # every queued item *before* cancellation. The yield budget
    # is larger than the burst so an unbounded queue would have
    # delivered all ``burst + 1`` items by the time cancel hits —
    # making the strict equality below fail in that regression.
    drain_can_run.set()
    for _ in range(burst + 100):
        await asyncio.sleep(0)
        if len(received) >= 1 + _DEFAULT_STREAM_QUEUE_MAX:
            break

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(received) == 1 + _DEFAULT_STREAM_QUEUE_MAX


async def test_push_priority_evicts_oldest_when_queue_full() -> None:
    """``push_priority`` lands even on full queue by evicting oldest.

    The terminal result + sentinel use this — they MUST reach the
    drain loop or the follower parks on ``queue.get`` forever
    after the producer is gone.
    """
    bus = EventBus()

    block = asyncio.Event()
    received: list[tuple[str, Any]] = []

    class BlockingClient:
        async def send_event(self, _mid: str, event: str, data: Any) -> None:
            received.append((event, data))
            if event == "fill":
                await block.wait()

    def _handle_event(event: Event, controls: StreamControls) -> None:
        if event.data.get("kind") == "priority":
            controls.push_priority("priority", event.data["i"])
            controls.end()
        else:
            controls.push("fill", event.data["i"])

    task = asyncio.create_task(
        stream_events(
            client=BlockingClient(),
            message_id="m1",
            bus=bus,
            event_types=[EventType.DEVICE_UPDATED],
            handle_event=_handle_event,
        )
    )
    await asyncio.sleep(0)

    # Park drain in send_event with the first fill.
    bus.fire(EventType.DEVICE_UPDATED, {"kind": "fill", "i": 0})
    await asyncio.sleep(0)

    # Fill the queue past capacity.
    for i in range(1, _DEFAULT_STREAM_QUEUE_MAX + 1):
        bus.fire(EventType.DEVICE_UPDATED, {"kind": "fill", "i": i})

    # Priority push: must evict to make room and land the result +
    # sentinel so end() unblocks the drain.
    bus.fire(EventType.DEVICE_UPDATED, {"kind": "priority", "i": 999})

    # Unblock drain so it can finish.
    block.set()

    await asyncio.wait_for(task, timeout=2.0)

    priority_events = [d for (e, d) in received if e == "priority"]
    assert priority_events == [999]


async def test_push_or_terminate_raises_when_queue_full() -> None:
    """``push_or_terminate`` makes the drain loop raise on overflow.

    State-tracking streams (``subscribe_events``) can't tolerate
    silent drops — a missed ``DEVICE_REMOVED`` leaves the dashboard
    showing a removed device until the user reconnects. Better to
    drop the connection so the WS handler closes, the client
    reconnects, and ``initial_state`` resyncs from scratch.

    This test parks the drain on the first event so the queue
    fills, fires once more to trigger the overflow path, then
    asserts the helper task raises ``StreamBackpressureError``
    after the drain unblocks. Without ``push_or_terminate`` the
    same shape would silently drop the overflow event.
    """
    bus = EventBus()

    drain_can_run = asyncio.Event()
    received: list[tuple[str, Any]] = []

    class GatedClient:
        async def send_event(self, _mid: str, event: str, data: Any) -> None:
            received.append((event, data))
            if len(received) == 1:
                await drain_can_run.wait()

    def _handle_event(event: Event, controls: StreamControls) -> None:
        controls.push_or_terminate(event.event_type.value, event.data["i"])

    task = asyncio.create_task(
        stream_events(
            client=GatedClient(),
            message_id="m1",
            bus=bus,
            event_types=[EventType.DEVICE_UPDATED],
            handle_event=_handle_event,
        )
    )
    await asyncio.sleep(0)

    # Park drain in send_event with the first event.
    bus.fire(EventType.DEVICE_UPDATED, {"i": 0})
    await asyncio.sleep(0)

    # Fill the queue exactly to capacity.
    for i in range(1, _DEFAULT_STREAM_QUEUE_MAX + 1):
        bus.fire(EventType.DEVICE_UPDATED, {"i": i})

    # One more fire — this is the one that triggers terminate.
    bus.fire(EventType.DEVICE_UPDATED, {"i": _DEFAULT_STREAM_QUEUE_MAX + 1})

    # Unblock the drain so it can process the queue and reach the
    # terminate sentinel that ``push_or_terminate`` enqueued.
    drain_can_run.set()

    with pytest.raises(StreamBackpressureError):
        await asyncio.wait_for(task, timeout=2.0)


async def test_push_or_terminate_does_not_raise_when_queue_has_room() -> None:
    """The terminate path only fires on actual overflow.

    Sanity: a normal fire under cap goes through ``push_or_terminate``
    and lands as a regular item — the helper drains it and parks
    again, no exception.
    """
    bus = EventBus()
    received: list[tuple[str, Any]] = []

    class _Client:
        async def send_event(self, _mid: str, event: str, data: Any) -> None:
            await asyncio.sleep(0)
            received.append((event, data))

    def _handle_event(event: Event, controls: StreamControls) -> None:
        controls.push_or_terminate(event.event_type.value, event.data["i"])
        if event.data["i"] == 1:
            controls.end()

    task = asyncio.create_task(
        stream_events(
            client=_Client(),
            message_id="m1",
            bus=bus,
            event_types=[EventType.DEVICE_UPDATED],
            handle_event=_handle_event,
        )
    )
    await asyncio.sleep(0)
    bus.fire(EventType.DEVICE_UPDATED, {"i": 0})
    bus.fire(EventType.DEVICE_UPDATED, {"i": 1})

    await asyncio.wait_for(task, timeout=1.0)
    assert received == [("device_updated", 0), ("device_updated", 1)]


async def test_force_enqueue_returns_when_get_nowait_finds_empty_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defensive ``QueueEmpty`` bail in ``_force_enqueue`` returns cleanly.

    ``_force_enqueue`` evicts the oldest item to make room for a
    must-land sentinel (terminal result, ``end()`` sentinel, the
    ``terminate`` marker). The eviction loop is::

        while True:
            try:
                queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    # Defensive: shouldn't happen given the
                    # synchronous listener path, but bail rather
                    # than spin if it does.
                    return

    In production this branch is unreachable — the queue can't be
    both full (``put_nowait`` raises ``QueueFull``) and empty
    (``get_nowait`` raises ``QueueEmpty``) at the same instant
    because the listener path is synchronous. But the defensive
    guard exists to prevent an infinite loop if a future refactor
    (or a peer task on the same loop) ever creates that race.

    Drives the branch by patching ``asyncio.Queue`` in the
    ``event_bus`` module to a stub that always raises
    ``QueueFull`` on ``put_nowait`` and ``QueueEmpty`` on
    ``get_nowait``. The stub's ``get()`` (the awaitable form
    used by the drain loop) returns the ``None`` sentinel so
    the drain returns cleanly — without that the test would
    hang because no terminal item ever lands.

    Pin the contract: ``controls.end()`` (which calls
    ``_force_enqueue(None)``) returns instead of spinning, the
    drain finds its sentinel, and ``stream_events`` exits.
    """
    from esphome_device_builder.helpers import event_bus

    class _ForeverFullEverEmptyQueue:
        """Always raises QueueFull on put + QueueEmpty on get_nowait.

        ``await get()`` returns ``None`` so the drain loop's
        ``while True: item = await queue.get(); if item is None:
        return`` exits cleanly — otherwise the test would hang
        waiting for a sentinel that ``put_nowait`` never accepted.
        """

        def __init__(self, maxsize: int = 0) -> None:
            pass

        def put_nowait(self, _item: Any) -> None:
            raise asyncio.QueueFull

        def get_nowait(self) -> Any:
            raise asyncio.QueueEmpty

        async def get(self) -> Any:
            return None

    monkeypatch.setattr(event_bus.asyncio, "Queue", _ForeverFullEverEmptyQueue)

    bus = EventBus()

    async def _send_initial(controls: StreamControls) -> None:
        # ``end()`` invokes ``_force_enqueue(None)``; with the
        # stub queue, put_nowait raises QueueFull, get_nowait
        # raises QueueEmpty, and the defensive branch returns.
        # The test is the absence of an infinite loop here —
        # ``asyncio.wait_for`` enforces that with a timeout.
        controls.end()

    def _handle_event(_event: Event, _controls: StreamControls) -> None:
        pass

    # If the defensive bail were missing, ``_force_enqueue`` would
    # spin forever and ``wait_for`` would time out. The 2s window
    # is generous enough for any realistic scheduler hiccup
    # without giving a regression a place to hide.
    await asyncio.wait_for(
        stream_events(
            client=_FakeClient(),
            message_id="m1",
            bus=bus,
            event_types=[EventType.DEVICE_UPDATED],
            handle_event=_handle_event,
            send_initial=_send_initial,
        ),
        timeout=2.0,
    )
