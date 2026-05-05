"""
End-to-end coverage for ``DevicesController.subscribe_reachability``.

Drives the per-device reachability subscription handler with a real
:class:`EventBus` + a real :class:`ReachabilityTracker` + a real
:class:`WebSocketClient` over a mock aiohttp WS. Pin the four
contract pieces:

1. **Initial snapshot** — on subscribe, the client receives one
   ``reachability_state`` event carrying the current per-signal
   freshness, then the ``{"subscribed": True}`` result confirmation.
2. **Per-device filter** — bus events for a *different* device do
   not reach this client.
3. **Live updates** — a fresh observation on the subscribed device
   pushes a follow-up ``reachability_state`` event.
4. **Cancel via stop_stream** — calling ``devices/stop_stream`` with
   the subscription's message_id cancels the handler task and the
   listener detaches (no leak on the bus).

Bonus:
5. ``device_name`` validation — missing or unknown device produces
   a typed ``CommandError`` rather than a silent stream that never
   delivers anything.
6. The mDNS refresh task only ticks when active source is mDNS —
   ping / mqtt-source devices don't get a 60s force-resolve.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.api.ws import WebSocketClient
from esphome_device_builder.controllers._reachability_tracker import (
    ReachabilityTracker,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import (
    Device,
    DeviceState,
    ErrorCode,
    EventType,
    ReachabilitySource,
)

from .conftest import MakeControllerFactory


def _make_ws_client() -> WebSocketClient:
    """Real ``WebSocketClient`` with a stub WS — exercises the real registry."""
    return WebSocketClient(MagicMock(), MagicMock(), authenticated=True)


def _record_sends(client: WebSocketClient) -> tuple[list[Any], list[Any]]:
    """Capture every ``send_event`` / ``send_result`` so tests can assert in order.

    The real ``WebSocketClient`` writes to its underlying aiohttp
    WS via ``send_json``. Replacing the public coroutines with
    list-append shims keeps the test off the network without
    swapping the whole class for ``FakeWebSocketClient`` (which
    doesn't implement the stream registry the handler needs).
    """
    events: list[tuple[str, str, Any]] = []
    results: list[tuple[str, Any]] = []

    async def send_event(message_id: str, event: str, data: Any) -> None:
        events.append((message_id, event, data))

    async def send_result(message_id: str, result: Any = None) -> None:
        results.append((message_id, result))

    client.send_event = send_event  # type: ignore[method-assign]
    client.send_result = send_result  # type: ignore[method-assign]
    return events, results


def _wire_reachability(controller: Any, tracker: ReachabilityTracker, bus: EventBus) -> None:
    """Stitch tracker + bus + state monitor stub onto a bypass-init controller.

    ``make_controller`` builds a minimal ``DevicesController`` that
    skips ``__init__``, so the reachability + bus wiring isn't
    there. Most tests don't care; these do.
    """
    controller._reachability = tracker
    controller._db.bus = bus
    # The handler reads ``priority_for`` on the state monitor to
    # decide whether to schedule the 60s refresh task. Default to
    # "ping" so the refresh-loop branch stays quiet (its no-op
    # path is what we want covered for most tests).
    state_monitor = MagicMock()
    state_monitor.priority_for = MagicMock(return_value=ReachabilitySource.PING)
    state_monitor.refresh_mdns = AsyncMock()
    controller._state_monitor = state_monitor


def _seed_device(controller: Any, name: str = "kitchen") -> Device:
    """Inject a single ``Device`` into the controller's name index."""
    device = Device(
        name=name,
        friendly_name=name.title(),
        configuration=f"{name}.yaml",
        address=f"{name}.local",
        ip="10.0.0.42",
        state=DeviceState.ONLINE,
    )
    controller._scanner.get_by_name = lambda n: [device] if n == name else []
    return device


async def _subscribe_and_wait(
    controller: Any,
    client: WebSocketClient,
    *,
    device_name: str,
    message_id: str,
    results: list[Any],
) -> asyncio.Task[None]:
    """Spawn the subscribe coroutine and wait for the initial confirmation.

    Returns the running task so the test can assert on its
    cancellation behaviour. By the time this returns, the
    handler has emitted the initial snapshot and is parked
    on the drain loop.
    """
    task = asyncio.create_task(
        controller.subscribe_reachability(
            device_name=device_name, client=client, message_id=message_id
        )
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if results:
            break
    assert results, "handler did not send subscription confirmation"
    return task


@pytest.mark.asyncio
async def test_subscribe_emits_initial_snapshot_then_confirmation(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """One ``reachability_state`` event lands before the ``subscribed`` result."""
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    _seed_device(controller)
    tracker.observe("kitchen", "ping")  # something to surface in the snapshot

    client = _make_ws_client()
    events, results = _record_sends(client)

    task = await _subscribe_and_wait(
        controller,
        client,
        device_name="kitchen",
        message_id="m1",
        results=results,
    )

    try:
        # Initial event preceded the result.
        assert len(events) == 1
        mid, event_name, data = events[0]
        assert mid == "m1"
        assert event_name == "reachability_state"
        assert data["device"] == "kitchen"
        assert data["ping_last_seen_seconds_ago"] is not None
        assert results == [("m1", {"subscribed": True})]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_live_event_for_subscribed_device_forwards(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Firing a ``DEVICE_REACHABILITY`` for the subscribed name pushes to the client."""
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    _seed_device(controller)

    client = _make_ws_client()
    events, results = _record_sends(client)

    task = await _subscribe_and_wait(
        controller,
        client,
        device_name="kitchen",
        message_id="m1",
        results=results,
    )

    try:
        bus.fire(
            EventType.DEVICE_REACHABILITY,
            {"device": "kitchen", "state": "online", "active_source": "mdns"},
        )
        # Drain pending event-loop callbacks until the handler enqueues
        # the live event into the queue and forwards it.
        for _ in range(50):
            await asyncio.sleep(0)
            if len(events) >= 2:
                break

        assert len(events) >= 2
        live_payload = events[1][2]
        assert live_payload["device"] == "kitchen"
        assert live_payload["active_source"] == "mdns"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_live_event_for_other_device_is_filtered(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A ``DEVICE_REACHABILITY`` for a different device must not leak in."""
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    _seed_device(controller)

    client = _make_ws_client()
    events, results = _record_sends(client)

    task = await _subscribe_and_wait(
        controller,
        client,
        device_name="kitchen",
        message_id="m1",
        results=results,
    )

    try:
        # Fire for a different device — should not surface.
        bus.fire(
            EventType.DEVICE_REACHABILITY,
            {"device": "garage", "state": "online", "active_source": "mdns"},
        )
        for _ in range(20):
            await asyncio.sleep(0)

        # Only the initial snapshot landed; the garage event was
        # rejected by the closure filter.
        assert len(events) == 1
        assert events[0][2]["device"] == "kitchen"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_subscribe_unknown_device_raises_not_found(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Unknown ``device_name`` surfaces as a typed NOT_FOUND."""
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    controller._scanner.get_by_name = lambda _name: []
    client = _make_ws_client()
    _record_sends(client)

    with pytest.raises(CommandError) as exc:
        await controller.subscribe_reachability(device_name="nope", client=client, message_id="m1")
    assert exc.value.code == ErrorCode.NOT_FOUND


@pytest.mark.asyncio
async def test_subscribe_missing_device_name_raises(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Empty ``device_name`` surfaces as a typed INVALID_MESSAGE."""
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    client = _make_ws_client()
    _record_sends(client)

    with pytest.raises(CommandError) as exc:
        await controller.subscribe_reachability(device_name="", client=client, message_id="m1")
    assert exc.value.code == ErrorCode.INVALID_MESSAGE


@pytest.mark.asyncio
async def test_cancel_via_stop_stream_detaches_listener(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``devices/stop_stream`` with the subscription's id cancels and unsubscribes.

    Locks down the unsubscribe contract: the bus has no leftover
    listeners, the handler task observes the cancel, and the
    register_stream entry is gone from the client.
    """
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    _seed_device(controller)
    client = _make_ws_client()
    _, results = _record_sends(client)

    task = await _subscribe_and_wait(
        controller,
        client,
        device_name="kitchen",
        message_id="m1",
        results=results,
    )

    # Listener is attached.
    assert len(bus._listeners.get(EventType.DEVICE_REACHABILITY, set())) == 1

    response = await controller.stop_stream(stream_id="m1", client=client)
    assert response == {"cancelled": True}
    with pytest.raises(asyncio.CancelledError):
        await task

    # Drain pending event-loop callbacks so the handler's
    # ``finally`` block (which detaches the listener and the
    # refresh task) runs.
    for _ in range(20):
        await asyncio.sleep(0)

    assert bus._listeners.get(EventType.DEVICE_REACHABILITY, set()) == set()
    # The stream registry entry was popped on cancel_stream.
    assert "m1" not in client._stream_tasks


@pytest.mark.asyncio
async def test_refresh_loop_only_calls_resolve_when_source_is_mdns(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Tick the 60s loop manually; mDNS-source ticks resolve, others don't.

    Drives the loop body directly instead of waiting 60 real
    seconds, then asserts the call pattern. Confirms the gate
    keeps the network quiet on ping/mqtt-source devices.
    """
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    _seed_device(controller)

    state_monitor = controller._state_monitor
    state_monitor.priority_for.side_effect = [
        ReachabilitySource.PING,
        ReachabilitySource.MDNS,
    ]

    # Patch sleep to exit the loop after two iterations so we don't
    # park for 60s real time. The third call raises ``CancelledError``
    # which the loop's ``except`` re-raises out — same shape as
    # production cancellation.
    iterations = 0

    async def fast_sleep(_: float) -> None:
        nonlocal iterations
        iterations += 1
        if iterations > 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError), pytest.MonkeyPatch.context() as m:
        m.setattr("asyncio.sleep", fast_sleep)
        await controller._reachability_refresh_loop("kitchen")

    # First tick: source was "ping" → no resolve. Second tick: "mdns"
    # → one resolve. The total count is 1 across the two ticks.
    assert state_monitor.refresh_mdns.await_count == 1
    state_monitor.refresh_mdns.assert_awaited_with("kitchen")
