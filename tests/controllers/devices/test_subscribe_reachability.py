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
from esphome_device_builder.controllers._device_scanner import ScanChange
from esphome_device_builder.controllers._device_state_monitor import (
    _MDNS_REFRESH_PADDING_SECONDS,
)
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


def _wire_reachability(
    controller: Any,
    tracker: ReachabilityTracker,
    bus: EventBus,
    *,
    wire_callback: bool = False,
) -> ReachabilityTracker:
    """Stitch tracker + bus + state monitor stub onto a bypass-init controller.

    ``make_controller`` builds a minimal ``DevicesController`` that
    skips ``__init__``, so the reachability + bus wiring isn't
    there. Most tests don't care; these do.

    ``wire_callback=True`` rebuilds the tracker with
    ``on_observation`` pointing at the controller's
    ``_on_reachability_observation`` so a ``tracker.observe(...)``
    call fans out to a real bus fire — exercises the production
    path end-to-end. Returns the tracker actually wired (a fresh
    instance when ``wire_callback`` is set, otherwise the one
    passed in).
    """
    if wire_callback:
        tracker = ReachabilityTracker(on_observation=controller._on_reachability_observation)
    controller._reachability = tracker
    controller._db.bus = bus
    # The handler reads ``priority_for`` on the state monitor to
    # decide whether to schedule the 60s refresh task. Default to
    # "ping" so the refresh-loop branch stays quiet (its no-op
    # path is what we want covered for most tests).
    state_monitor = MagicMock()
    state_monitor.priority_for = MagicMock(return_value=ReachabilitySource.PING)
    state_monitor.refresh_mdns = AsyncMock()
    # The refresh loop reads ``get_mdns_a_record_ttl_remaining``
    # to decide how long to sleep before the next probe.
    # Returning ``None`` makes it sleep just the padding (~1s);
    # a default MagicMock would raise ``TypeError`` on the ``+``
    # arithmetic.
    state_monitor.get_mdns_a_record_ttl_remaining = MagicMock(return_value=None)
    controller._state_monitor = state_monitor
    return tracker


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


async def test_subscribe_without_client_returns_silently(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """No ``client`` arg → early return, no exception, no work.

    Mirrors ``stop_stream``'s early-return guard. Dispatch flows
    that don't carry a per-connection client (legacy REST shim,
    programmatic callers) shouldn't crash on subscribe — they
    just get a no-op back.
    """
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    _seed_device(controller)

    # No raise; nothing on the bus.
    await controller.subscribe_reachability(device_name="kitchen", message_id="m1")
    assert bus._listeners.get(EventType.DEVICE_REACHABILITY, set()) == set()


def test_observation_fires_bus_event_for_known_device(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A real ``tracker.observe`` fans out to a ``DEVICE_REACHABILITY`` bus event.

    Wires the tracker's ``on_observation`` callback all the way to
    the controller's ``_on_reachability_observation`` (production
    shape) so the assertion is on the actual bus fire — not on the
    intermediate snapshot helper. Pins the contract that an
    observation produces exactly one bus event with the wire-shape
    payload.
    """
    controller = make_controller(tmp_path)
    bus = EventBus()
    _seed_device(controller)
    tracker = _wire_reachability(controller, ReachabilityTracker(), bus, wire_callback=True)
    fired: list[Any] = []
    bus.add_listener(EventType.DEVICE_REACHABILITY, fired.append)

    # Use a ping observation — mDNS is cache-driven and the
    # bypass-init test fixture doesn't wire a cache reader, so an
    # mDNS observe would still fire the callback but the snapshot
    # would have null mDNS fields. Ping stamps directly so we can
    # also pin the snapshot's payload shape end-to-end.
    tracker.observe("kitchen", "ping")

    assert len(fired) == 1
    assert fired[0].data["device"] == "kitchen"
    assert fired[0].data["ping_last_seen_seconds_ago"] is not None


def test_observation_for_deleted_device_is_dropped(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An observation that races a device deletion fires no event.

    The state monitor's per-source maps live on the tracker, not
    keyed against the device catalog — so a stale observation can
    arrive after the YAML is gone. The controller's
    ``_on_reachability_observation`` looks the name up in the
    scanner; on miss it bails out instead of firing an event
    with a half-built snapshot.
    """
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    # Scanner has no devices.
    controller._scanner.get_by_name = lambda _name: []
    fired: list[Any] = []
    bus.add_listener(EventType.DEVICE_REACHABILITY, fired.append)

    controller._on_reachability_observation("ghost")
    assert fired == []


def test_scan_change_removed_clears_tracker(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Deleting a device's YAML clears its per-signal tracker entries.

    Without this, the four maps would accumulate one row per device
    that ever lived in the catalog. The mDNS browser's ``Removed``
    branch clears the tracker for the *broadcast* gone case, but
    YAML deletion has its own scan-change path.
    """
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    device = _seed_device(controller)
    tracker.observe("kitchen", "mdns")
    tracker.observe("kitchen", "ping")

    # Stub ``revisit_all_importables`` (called on REMOVED) so we
    # don't have to hand-build an import_discovery. The tracker
    # clear is what we're pinning down.
    controller._state_monitor.revisit_all_importables = MagicMock()
    controller.state.regenerate_failed = set()

    controller._on_scan_change(ScanChange.REMOVED, device)

    snap = tracker.snapshot("kitchen", state=DeviceState.OFFLINE, active_source="unknown", ip="")
    assert snap["mdns_last_seen_seconds_ago"] is None
    assert snap["ping_last_seen_seconds_ago"] is None


async def test_refresh_loop_only_calls_resolve_when_source_is_mdns(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Tick the loop manually; mDNS-source ticks resolve, others don't.

    Drives the loop body directly instead of waiting real seconds,
    then asserts the call pattern. Confirms the gate keeps the
    network quiet on ping/mqtt-source devices.
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
    # park for real time. The third call raises ``CancelledError``,
    # same shape as production cancellation.
    iterations = 0

    async def fast_sleep(_: float) -> None:
        nonlocal iterations
        iterations += 1
        if iterations > 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError), pytest.MonkeyPatch.context() as m:
        m.setattr("asyncio.sleep", fast_sleep)
        await controller._reachability_refresh_loop("kitchen")

    # First iteration: sleep, then source = ping → no refresh.
    # Second iteration: sleep, then source = mdns → one refresh.
    # Third iteration: sleep raises CancelledError before the
    # priority probe runs. Total: 1 refresh across two ticks.
    assert state_monitor.refresh_mdns.await_count == 1
    state_monitor.refresh_mdns.assert_awaited_with("kitchen")


async def test_refresh_loop_sleeps_until_cache_expiry(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Sleep duration tracks the cached A record's remaining TTL.

    The cache-aware sleep is the whole point of the redesign:
    ``async_resolve_host`` short-circuits on cache hit, so a
    fixed-interval probe within the cache's lifetime would
    never go on the wire. Sleeping ``ttl_remaining + padding``
    means the next iteration runs after the cache record has
    aged past expiry, the cache check fails, and the wire query
    fires for real.
    """
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    _seed_device(controller)
    state_monitor = controller._state_monitor
    # Two A-TTL reads cover the two iterations: first fresh
    # (90s remaining), then expired (None — typical post-refresh
    # state if the device didn't respond).
    state_monitor.get_mdns_a_record_ttl_remaining.side_effect = [90.0, None]
    state_monitor.priority_for.return_value = ReachabilitySource.MDNS

    sleep_durations: list[float] = []
    iterations = 0

    async def recording_sleep(delay: float) -> None:
        nonlocal iterations
        sleep_durations.append(delay)
        iterations += 1
        if iterations >= 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError), pytest.MonkeyPatch.context() as m:
        m.setattr("asyncio.sleep", recording_sleep)
        await controller._reachability_refresh_loop("kitchen")

    # First sleep: TTL=90s + padding.
    # Second sleep: cache empty (info=None) → padding.
    assert sleep_durations[0] == pytest.approx(90.0 + _MDNS_REFRESH_PADDING_SECONDS)
    assert sleep_durations[1] == pytest.approx(_MDNS_REFRESH_PADDING_SECONDS)


async def test_refresh_loop_skips_wire_query_when_recheck_finds_fresh_cache(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An mDNS announce arriving during the sleep re-arms the cache; no wire query.

    Sequence:
      1. Cache TTL=90s → sleep 91s.
      2. Recheck finds fresh cache (a passive announce landed
         during our sleep, e.g. ``ttl_remaining=60s``) → sleep
         again, **no wire query**.
      3. Recheck finds expired cache → wire query fires.

    Pin that the wire query is gated on "still expired at
    recheck time," not "we slept based on an earlier reading."
    Without this, an unrelated mDNS announce landing during the
    sleep would still get clobbered by a redundant wire query
    on wake-up.
    """
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    _seed_device(controller)
    state_monitor = controller._state_monitor
    state_monitor.priority_for.return_value = ReachabilitySource.MDNS
    # Three A-TTL reads: fresh (90s) → fresh again (60s, an
    # announce arrived) → empty (cache evicted).
    state_monitor.get_mdns_a_record_ttl_remaining.side_effect = [90.0, 60.0, None]

    sleep_count = 0

    async def fast_sleep(_: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        # Cancel after the third sleep — by then we've covered
        # the two fresh-cache iterations + the start of the
        # expired-cache iteration's padding sleep, but
        # ``CancelledError`` raised here preempts the wire
        # query on this iteration. The assertion below confirms
        # ``refresh_mdns`` was never awaited.
        if sleep_count >= 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError), pytest.MonkeyPatch.context() as m:
        m.setattr("asyncio.sleep", fast_sleep)
        await controller._reachability_refresh_loop("kitchen")

    state_monitor.refresh_mdns.assert_not_awaited()


async def test_get_reachability_returns_current_snapshot(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """The one-shot poll command returns the same snapshot subscribe seeds."""
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    _seed_device(controller)
    tracker.observe("kitchen", "ping")

    snap = await controller.get_reachability(device_name="kitchen")
    assert snap["device"] == "kitchen"
    assert snap["ping_last_seen_seconds_ago"] is not None
    # Same wire shape as the subscribe seed (PTR-TTL field included).
    assert snap.keys() == controller.get_reachability_snapshot("kitchen").keys()
    assert "mdns_ptr_ttl_remaining_seconds" in snap


async def test_get_reachability_unknown_device_raises_not_found(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Unknown ``device_name`` surfaces as a typed NOT_FOUND."""
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)
    controller._scanner.get_by_name = lambda _name: []

    with pytest.raises(CommandError) as exc:
        await controller.get_reachability(device_name="nope")
    assert exc.value.code == ErrorCode.NOT_FOUND


async def test_get_reachability_missing_device_name_raises(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Empty ``device_name`` surfaces as a typed INVALID_MESSAGE, mirroring subscribe."""
    controller = make_controller(tmp_path)
    tracker = ReachabilityTracker()
    bus = EventBus()
    _wire_reachability(controller, tracker, bus)

    with pytest.raises(CommandError) as exc:
        await controller.get_reachability(device_name="")
    assert exc.value.code == ErrorCode.INVALID_MESSAGE
