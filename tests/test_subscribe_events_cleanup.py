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
from esphome_device_builder.helpers.event_bus import (
    _DEFAULT_STREAM_QUEUE_MAX,
    EventBus,
    StreamBackpressureError,
)
from esphome_device_builder.models import EventType

from .conftest import FakeWebSocketClient


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
    client = FakeWebSocketClient()

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


async def test_subscribe_events_excludes_device_reachability() -> None:
    """``DEVICE_REACHABILITY`` events do not reach broadcast subscribers.

    The per-device ``devices/subscribe_reachability`` stream is the
    only intended consumer of these events. Without the explicit
    exclusion in ``_cmd_subscribe_events``, every freshness ping
    (mDNS announce, ICMP success, MQTT discover) would broadcast
    to every connected client — pinning the bounded queue's
    backpressure terminator at fleet scale and tearing the
    connection down.
    """
    db = _make_db()
    client = FakeWebSocketClient()

    handler_task = asyncio.create_task(db._cmd_subscribe_events(client=client, message_id="m1"))
    for _ in range(50):
        await asyncio.sleep(0)
        if client.results:
            break

    # Fire a reachability event — the broadcast subscriber should
    # *not* see it. Sanity-check with a normal event afterwards
    # so a regression that drops everything (not just
    # reachability) shows up too.
    db.bus.fire(
        EventType.DEVICE_REACHABILITY,
        {"device": "kitchen", "active_source": "mdns"},
    )
    db.bus.fire(EventType.DEVICE_UPDATED, {"device": MagicMock(to_dict=lambda: {"x": 1})})
    for _ in range(10):
        await asyncio.sleep(0)
        if client.events:
            break

    # Only the device_updated event landed — no reachability_state.
    event_names = [name for (_mid, name, _data) in client.events]
    assert "device_reachability" not in event_names
    assert "device_updated" in event_names

    handler_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler_task


async def test_subscribe_events_listener_forwards_bus_events() -> None:
    """While parked, fired bus events reach the client as send_event calls.

    Locks the actual subscription behaviour the handler is meant
    to provide — without this, the cleanup-on-cancel test could
    pass against a do-nothing handler that just attaches and
    detaches listeners without forwarding.
    """
    db = _make_db()
    client = FakeWebSocketClient()

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

        The default ``FakeWebSocketClient`` returns synchronously, so the
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


class _GatedClient:
    """Records sends; parks the drain on the first call.

    Lifted to module scope so the test loop body doesn't define
    a class that closes over per-iteration variables (ruff B023
    flags that pattern, and the closure can silently bind the
    wrong iteration's variables when reading them).
    """

    def __init__(
        self,
        received: list[tuple[str, str, Any]],
        drain_can_run: asyncio.Event,
    ) -> None:
        self._received = received
        self._drain_can_run = drain_can_run
        self.results: list[tuple[str, Any]] = []

    async def send_event(self, message_id: str, event: str, data: Any) -> None:
        self._received.append((message_id, event, data))
        if len(self._received) == 1:
            await self._drain_can_run.wait()

    async def send_result(self, message_id: str, result: Any) -> None:
        self.results.append((message_id, result))


@pytest.mark.parametrize(
    ("event_type", "payload_factory"),
    [
        (EventType.JOB_OUTPUT, lambda: {"job_id": "a", "line": "x"}),
        (
            EventType.DEVICE_UPDATED,
            lambda: {"device": MagicMock(to_dict=lambda: {"id": "x"})},
        ),
    ],
    ids=["job_output", "device_updated"],
)
async def test_subscribe_events_terminates_on_overflow_uniformly(
    event_type: EventType,
    payload_factory: Any,
) -> None:
    """Every event type fails closed when the queue overflows.

    A client that's fallen 4000+ events behind is already broken
    — its UI is showing wildly stale data either way. The
    cleanest recovery is to drop the connection (via
    ``push_or_terminate``) so the client reconnects and reseeds
    device state from ``initial_state``; for authoritative job
    state the client uses ``follow_jobs`` (which has its own
    snapshot). Selectively keeping log lines or lifecycle
    events through an overflow doesn't actually leave the UI
    in a usable state — it just adds policy complexity for no
    user-visible win.

    Parametrised over both event families (a JOB_* event and a
    DEVICE_* event) so a future regression that re-introduces a
    per-event-type policy surfaces here regardless of which
    path it touched.
    """
    db = _make_db()
    drain_can_run = asyncio.Event()
    received: list[tuple[str, str, Any]] = []
    client = _GatedClient(received, drain_can_run)

    handler_task = asyncio.create_task(db._cmd_subscribe_events(client=client, message_id="m1"))
    await asyncio.sleep(0)

    # Park drain on the first event so the queue can fill.
    payload = payload_factory()
    db.bus.fire(event_type, payload)
    await asyncio.sleep(0)

    # Fill past the cap.
    for _ in range(_DEFAULT_STREAM_QUEUE_MAX + 1):
        db.bus.fire(event_type, payload)

    drain_can_run.set()
    # The helper must raise StreamBackpressureError once the
    # drain reaches the terminate sentinel — same outcome
    # for any event type.
    with pytest.raises(StreamBackpressureError):
        await asyncio.wait_for(handler_task, timeout=2.0)
