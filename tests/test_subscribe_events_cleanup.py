"""Tests for ``DeviceBuilder._cmd_subscribe_events`` listener cleanup.

Pin down the contract that subscriptions are released when the WS
task is cancelled (which is what happens when a client disconnects
— ``WebSocketClient.cleanup`` cancels every tracked task it
owns). Without this, every WS reconnect leaked ~one listener per
``EventType`` per disconnected client; the closures held a
reference to the closed client, so every subsequent ``bus.fire``
iterated dead listeners and tried to ``send_event`` on a closed
connection (raising every time, caught + logged by
``bus.fire``'s exception handler).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.device_builder import DeviceBuilder
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import EventType


class _FakeClient:
    """Minimal WebSocketClient stand-in for the subscribe_events handler."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, Any]] = []
        self.results: list[tuple[str, Any]] = []

    async def send_event(self, message_id: str, event: str, data: Any) -> None:
        self.events.append((message_id, event, data))

    async def send_result(self, message_id: str, result: Any) -> None:
        self.results.append((message_id, result))


def _make_db() -> DeviceBuilder:
    """Build a minimally-initialised DeviceBuilder for the handler.

    Only ``self.bus`` and ``self.devices`` are read by
    ``_cmd_subscribe_events``; everything else can be a stub.
    """
    db = DeviceBuilder.__new__(DeviceBuilder)
    db.bus = EventBus()
    db.devices = None  # skip the initial-snapshot branch
    return db


async def test_subscribe_events_unsubscribes_on_cancellation() -> None:
    """Cancelling the handler task triggers the ``with`` cleanup.

    Drives the real handler through a real ``EventBus``, parks it,
    then cancels the task and asserts no listeners remain on the
    bus. Without the ``with bus.listening`` context, the original
    code returned after sending the subscription confirmation and
    left every listener attached forever.
    """
    db = _make_db()
    client = _FakeClient()

    handler_task = asyncio.create_task(db._cmd_subscribe_events(client=client, message_id="m1"))

    # Wait for the handler to send its subscription confirmation —
    # at that point the listeners are attached and the handler is
    # parked on ``asyncio.Event().wait()``.
    for _ in range(50):
        await asyncio.sleep(0)
        if client.results:
            break
    assert client.results == [("m1", {"subscribed": True})]

    # Listeners are attached for every EventType.
    listener_count_before = sum(len(db.bus._listeners.get(et, ())) for et in EventType)
    assert listener_count_before > 0, "no listeners attached during subscription"

    # Cancel the task — this is what ``WebSocketClient.cleanup`` does
    # when the WS connection closes.
    handler_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler_task

    # Every listener attached by the handler should now be gone.
    listener_count_after = sum(len(db.bus._listeners.get(et, ())) for et in EventType)
    assert listener_count_after == 0, (
        f"listener leak: {listener_count_after} listener(s) still attached "
        f"after cancellation (was {listener_count_before} during the run)"
    )


async def test_subscribe_events_listener_forwards_bus_events() -> None:
    """While parked, fired bus events reach the client as send_event calls.

    Locks the actual subscription behaviour the handler is meant
    to provide — without this, the cleanup-on-cancel test could
    pass against a do-nothing handler that just attaches and
    detaches listeners without forwarding.
    """
    db = _make_db()
    client = _FakeClient()

    handler_task = asyncio.create_task(db._cmd_subscribe_events(client=client, message_id="m1"))

    # Wait for the subscription to confirm.
    for _ in range(50):
        await asyncio.sleep(0)
        if client.results:
            break

    # Fire a bus event — the listener should forward it to the
    # client via send_event.
    db.bus.fire(EventType.DEVICE_UPDATED, {"device": MagicMock(to_dict=lambda: {"x": 1})})

    # Yield so the helper's drain loop picks up the queued event.
    for _ in range(10):
        await asyncio.sleep(0)
        if client.events:
            break

    assert client.events == [("m1", "device_updated", {"device": {"x": 1}})]

    # Clean up the parked task so the test finishes.
    handler_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler_task


async def test_subscribe_events_subscribed_arrives_before_live_events() -> None:
    """Initial state and ``subscribed`` confirm precede a live event fired mid-seed.

    Locks the snapshot/live ordering contract. Without an actual
    in-flight await during ``send_initial``, the listener has
    nowhere to interleave and the test passes against a regressed
    implementation that never serialised the snapshot ahead of the
    live event. The setup here gives ``_send_initial`` a real
    ``initial_state`` payload to send and a fake ``send_event`` /
    ``send_result`` that yield via ``asyncio.sleep(0)`` — so a
    fired event has at least one yield window during which it
    must be queued, then drained strictly after the seed.
    """
    db = DeviceBuilder.__new__(DeviceBuilder)
    db.bus = EventBus()

    devices_mock = MagicMock()
    devices_mock.get_devices.return_value = []
    devices_mock.get_importable_devices.return_value = []
    db.devices = devices_mock

    class YieldingClient:
        """``send_event`` / ``send_result`` actually yield the loop.

        The default ``_FakeClient`` returns synchronously, so the
        handler's ``send_initial`` would never yield and a fired
        event would arrive *after* parking — turning this from an
        ordering test into a "drain delivers what was fired"
        test that doesn't pin the seed-vs-live race at all.
        """

        def __init__(self) -> None:
            self.events: list[tuple[str, str, Any]] = []
            self.results: list[tuple[str, Any]] = []

        async def send_event(self, message_id: str, event: str, data: Any) -> None:
            await asyncio.sleep(0)
            self.events.append((message_id, event, data))

        async def send_result(self, message_id: str, result: Any) -> None:
            await asyncio.sleep(0)
            self.results.append((message_id, result))

    client = YieldingClient()
    handler_task = asyncio.create_task(db._cmd_subscribe_events(client=client, message_id="m1"))
    # Yield once so listeners attach and send_initial starts
    # awaiting send_event for ``initial_state``.
    await asyncio.sleep(0)

    # Fire a live event while the seed is still in flight (the
    # ``initial_state`` send_event has not yet appended). The
    # listener must queue this; the helper's drain must deliver it
    # only after both ``initial_state`` and ``subscribed`` land.
    db.bus.fire(EventType.DEVICE_UPDATED, {"device": MagicMock(to_dict=lambda: {"y": 2})})

    for _ in range(50):
        await asyncio.sleep(0)
        if client.results and any(e == "device_updated" for (_m, e, _d) in client.events):
            break

    # Strict ordering: the seed's initial_state event arrives
    # first, then the subscribed confirm via send_result, then
    # the live device_updated event via the drain loop.
    assert client.events[0][1] == "initial_state"
    assert client.results == [("m1", {"subscribed": True})]
    assert client.events[-1] == ("m1", "device_updated", {"device": {"y": 2}})

    handler_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler_task


async def test_subscribe_events_drops_job_events_silently_on_overflow() -> None:
    """JOB_* events through subscribe_events use lossy ``push``, not terminate.

    ``subscribe_events`` doesn't reseed jobs in ``initial_state``
    (only devices + importable). Forcing a disconnect on a JOB_*
    overflow would tell the client to reconnect, but the reseed
    can't recover the missed job state — clients that need
    reliable job tracking use ``follow_jobs`` (which has its own
    snapshot). So JOB_* events go through lossy ``push`` instead
    of fail-closed ``push_or_terminate``: the alternative would
    tear the connection down without buying recovery.

    This test fills the queue past the cap with JOB_OUTPUT events
    while the drain is parked, then asserts the helper task does
    NOT raise — the overflow is silently dropped instead.
    """
    from esphome_device_builder.helpers.event_bus import (
        _DEFAULT_STREAM_QUEUE_MAX,
        StreamBackpressureError,
    )

    db = _make_db()

    drain_can_run = asyncio.Event()
    received: list[tuple[str, str, Any]] = []

    class GatedClient:
        def __init__(self) -> None:
            self.results: list[tuple[str, Any]] = []

        async def send_event(self, message_id: str, event: str, data: Any) -> None:
            received.append((message_id, event, data))
            if len(received) == 1:
                await drain_can_run.wait()

        async def send_result(self, message_id: str, result: Any) -> None:
            self.results.append((message_id, result))

    client = GatedClient()
    handler_task = asyncio.create_task(db._cmd_subscribe_events(client=client, message_id="m1"))
    await asyncio.sleep(0)

    # Park drain on first event (a JOB_* event so the lossy path
    # is the one being exercised).
    db.bus.fire(EventType.JOB_OUTPUT, {"job_id": "a", "line": "first\n"})
    await asyncio.sleep(0)

    # Fire well past the cap. With lossy push these all silently
    # drop on QueueFull; no terminate sentinel is enqueued.
    for i in range(_DEFAULT_STREAM_QUEUE_MAX + 500):
        db.bus.fire(EventType.JOB_OUTPUT, {"job_id": "a", "line": f"l{i}\n"})

    drain_can_run.set()
    # Yield generously so the drain processes the backlog. If a
    # terminate sentinel had snuck into the queue this would
    # raise; the assertion below catches the regression.
    for _ in range(_DEFAULT_STREAM_QUEUE_MAX + 100):
        await asyncio.sleep(0)
        if len(received) >= 1 + _DEFAULT_STREAM_QUEUE_MAX:
            break

    handler_task.cancel()
    try:
        await handler_task
    except asyncio.CancelledError:
        pass
    except StreamBackpressureError:
        pytest.fail(
            "subscribe_events raised StreamBackpressureError on a JOB_* "
            "overflow — JOB_* events must use lossy push (no reseed path)"
        )

    # Strict equality: drop-newest delivers exactly cap + 1
    # (the parked first event). Terminate would surface as the
    # exception above; an unbounded queue would deliver every
    # fired item.
    assert len(received) == 1 + _DEFAULT_STREAM_QUEUE_MAX


async def test_subscribe_events_terminates_on_device_event_overflow() -> None:
    """DEVICE_* events use fail-closed ``push_or_terminate``.

    Mirror of the JOB_* test above for the resync-able half of
    ``subscribe_events``: device-state changes carry transitions
    the UI tracks against ``initial_state.devices`` /
    ``initial_state.importable``. A silent drop here would leave
    the dashboard permanently stale (still showing a removed
    device, missing a "device went online"). Forcing the WS to
    close so the client reconnects and reseeds is the only
    correct recovery — and the matching dispatch wiring closes
    the WS via ``schedule_close``.
    """
    from esphome_device_builder.helpers.event_bus import (
        _DEFAULT_STREAM_QUEUE_MAX,
        StreamBackpressureError,
    )

    db = _make_db()

    drain_can_run = asyncio.Event()
    received: list[tuple[str, str, Any]] = []

    class GatedClient:
        def __init__(self) -> None:
            self.results: list[tuple[str, Any]] = []

        async def send_event(self, message_id: str, event: str, data: Any) -> None:
            received.append((message_id, event, data))
            if len(received) == 1:
                await drain_can_run.wait()

        async def send_result(self, message_id: str, result: Any) -> None:
            self.results.append((message_id, result))

    client = GatedClient()
    handler_task = asyncio.create_task(db._cmd_subscribe_events(client=client, message_id="m1"))
    await asyncio.sleep(0)

    # Park drain on a DEVICE_UPDATED so the fail-closed path is
    # the one exercised by the overflow.
    device_payload = MagicMock(to_dict=lambda: {"id": "x"})
    db.bus.fire(EventType.DEVICE_UPDATED, {"device": device_payload})
    await asyncio.sleep(0)

    # Fill past the cap.
    for _ in range(_DEFAULT_STREAM_QUEUE_MAX + 1):
        db.bus.fire(EventType.DEVICE_UPDATED, {"device": device_payload})

    drain_can_run.set()
    # The helper must raise StreamBackpressureError once the
    # drain reaches the terminate sentinel.
    with pytest.raises(StreamBackpressureError):
        await asyncio.wait_for(handler_task, timeout=2.0)
