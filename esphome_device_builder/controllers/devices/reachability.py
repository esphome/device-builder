"""
Per-device reachability streaming + on-demand mDNS refresh.

Drawer-only: while the device drawer is open the frontend opens
a ``devices/subscribe_reachability`` stream so it can show
"mDNS heard 12s ago, ping 47s ago, MQTT 2 min ago, RTT 4 ms"
without bloating the broadcast ``subscribe_events`` channel for
every other connected client. While subscribed AND the device's
active source is mDNS, an A-record refresh loop runs alongside
the stream so the displayed "last seen" age doesn't grow
unboundedly past the cached TTL.

The controller keeps thin bound-method delegates
(``subscribe_reachability``, ``_reachability_refresh_loop``,
``_build_reachability_snapshot``, ``_on_reachability_observation``,
``get_reachability_snapshot``, ``refresh_device_mdns``) so the
WS dispatch, the :class:`ReachabilityTracker` observation
callback, and tests that call them as instance methods all
resolve to the same names.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.event_bus import Event, StreamControls, stream_events
from ...models import (
    DeviceReachabilityData,
    ErrorCode,
    EventType,
    ReachabilitySource,
)
from .._device_state_monitor import _MDNS_REFRESH_PADDING_SECONDS

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


async def subscribe(
    controller: DevicesController,
    *,
    device_name: str,
    client: Any,
    message_id: str,
) -> None:
    """
    Stream per-signal reachability for a single device.

    Wire shape:
      → ``{"command": "devices/subscribe_reachability",
            "message_id": "<id>",
            "args": {"device_name": "kitchen"}}``
      ← ``{"event": "reachability_state", "message_id": "<id>",
            "data": <ReachabilitySnapshot>}``  (initial + on every change)
      ← ``{"result": {"subscribed": true}, "message_id": "<id>"}``
      → ``{"command": "devices/stop_stream",
            "args": {"stream_id": "<id>"}}``  (to end the stream)

    While subscribed AND the device's active source is mDNS,
    the backend force-refreshes the A record every 60s so a
    stale broadcast doesn't keep the displayed "last seen" age
    growing forever. Ping-source devices are already covered by
    the regular ping sweep; MQTT-source by the discover-publish
    loop. Both feed the tracker through the same path the
    initial subscription read.
    """
    if client is None:
        return
    if not device_name:
        raise CommandError(ErrorCode.INVALID_MESSAGE, "device_name is required")
    if controller.get_reachability_snapshot(device_name) is None:
        raise CommandError(ErrorCode.NOT_FOUND, f"No configured device named {device_name!r}")

    # Register so a peer ``devices/stop_stream`` (or this client's
    # cleanup on disconnect) cancels the running task.
    task = asyncio.current_task()
    assert task is not None
    client.register_stream(message_id, task)

    refresh_task: asyncio.Task | None = None

    async def _send_initial(controls: StreamControls) -> None:
        snapshot = controller.get_reachability_snapshot(device_name)
        if snapshot is not None:
            await client.send_event(message_id, "reachability_state", snapshot)
        await client.send_result(message_id, {"subscribed": True})

    def _handle_event(event: Event[DeviceReachabilityData], controls: StreamControls) -> None:
        data = event.data
        if data["device"] != device_name:
            # The bus event is broadcast (one listener for every
            # subscriber); filter inside the closure so each
            # subscriber only forwards the events for its device.
            return
        controls.push("reachability_state", data)

    try:
        # Spawn the 60s mDNS refresh loop alongside the stream
        # so it gets cancelled together with the subscription
        # when the WS disconnects or ``devices/stop_stream``
        # cancels this task. Routed through the controller's
        # bound delegate so tests patching the loop on the
        # instance still intercept.
        refresh_task = asyncio.create_task(controller._reachability_refresh_loop(device_name))
        await stream_events(
            client=client,
            message_id=message_id,
            bus=controller._db.bus,
            event_types=[EventType.DEVICE_REACHABILITY],
            handle_event=_handle_event,
            send_initial=_send_initial,
        )
    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task
        client.unregister_stream(message_id)


async def refresh_loop(controller: DevicesController, device_name: str) -> None:
    """Schedule mDNS refreshes off the cached A record's expiry.

    Quiet when active source is ping (the regular sweep already
    runs every 60s) or MQTT (the discover-publish loop already
    ticks every 2s).

    Why scheduled-on-expiry rather than fixed-interval: the
    canonical ``async_resolve_host`` short-circuits on cache
    hit (``_load_from_cache`` returns the cached value if
    the record is present and not expired), so a
    fixed-interval probe within the cache's lifetime
    wouldn't actually go on the wire — we'd just keep
    re-reading the same cached entry until it eventually
    ages out and the next iteration finally reaches
    ``async_request``.

    On every iteration, re-read the cached A record's
    remaining TTL. If a fresh entry is alive, sleep until it
    ages out (``ttl_remaining + padding``) then loop —
    rechecking after the sleep handles the case where an
    unrelated mDNS announce reached us during the sleep
    window and re-armed the cache; we just sleep again for
    the new lifetime instead of issuing a redundant query.
    Only when the recheck shows expired / absent does the
    wire query fire — by then ``_load_from_cache`` will fail
    and ``async_resolve_host`` will actually go on the wire.
    ESPHome devices are mDNS-silent except in response to
    probes; ``ServiceBrowser`` only keeps the PTR record
    (4500s TTL) alive, not A/AAAA (120s). Without this loop
    the A record decays unrecoverably 120s after the most
    recent probe.
    """
    while True:
        # Use the A/AAAA-specific TTL — not the union-of-types
        # ``get_mdns_cache_info``: PTR has a 4500s TTL and
        # stays cached for ages, so a sleep keyed on it
        # would never wake up to refresh A. We're driving
        # the loop off the A record's much shorter 120s
        # decay because that's the one we actually need to
        # keep alive for the drawer's freshness display.
        a_ttl_remaining = controller._state_monitor.get_mdns_a_record_ttl_remaining(device_name)
        if a_ttl_remaining is not None and a_ttl_remaining > 0:
            # A still alive — sleep until just past expiry,
            # then re-check rather than probing immediately.
            # A fresh announce arriving during the sleep
            # would re-arm the cache and the recheck spares
            # us a redundant wire query.
            await asyncio.sleep(a_ttl_remaining + _MDNS_REFRESH_PADDING_SECONDS)
            continue
        # A expired or absent — probe the wire to refresh
        # it. The padding before the first probe also gives
        # the subscription's initial snapshot a chance to
        # land before we issue our first query.
        await asyncio.sleep(_MDNS_REFRESH_PADDING_SECONDS)
        if controller._state_monitor.priority_for(device_name) is ReachabilitySource.MDNS:
            await controller.refresh_device_mdns(device_name)


def build_snapshot(controller: DevicesController, name: str) -> DeviceReachabilityData | None:
    """
    Stitch state + tracker fields into the reachability wire shape.

    The state monitor owns ``state`` / ``active_source`` / ``ip``;
    the tracker owns the per-signal freshness fields. Both
    ``get_reachability_snapshot`` (initial WS subscribe) and
    ``on_observation`` (per-event push) need the merged dict,
    so the device-lookup + delegate-to-tracker combo lives once
    here. Returns ``None`` when no configured device matches
    *name*.
    """
    bucket = controller._scanner.get_by_name(name)
    if not bucket:
        return None
    first = bucket[0]
    return controller._reachability.snapshot(
        name,
        state=first.state,
        active_source=controller._state_monitor.priority_for(name),
        ip=first.ip,
    )


def on_observation(controller: DevicesController, name: str) -> None:
    """
    Forward a reachability freshness observation onto the event bus.

    Fires :data:`EventType.DEVICE_REACHABILITY` carrying the full
    wire-shape snapshot for *name*. The device drawer's per-device
    subscription filters by ``data["device"]`` and pushes the
    snapshot to the client. The event is *not* forwarded by the
    broadcast ``subscribe_events`` channel — adding a periodic
    per-device freshness ping to every connected client would
    bloat the bus for no UI gain.
    """
    snapshot = controller._build_reachability_snapshot(name)
    if snapshot is None:
        return
    controller._db.bus.fire(EventType.DEVICE_REACHABILITY, snapshot)


async def refresh_device_mdns(controller: DevicesController, name: str) -> None:
    """Force-refresh a device's mDNS A record. No-op if zeroconf is down."""
    await controller._state_monitor.refresh_mdns(name)
