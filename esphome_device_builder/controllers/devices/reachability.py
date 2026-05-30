"""Per-device reachability streaming + on-demand mDNS refresh."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError, registered_stream
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

    Drawer-only: the per-device freshness display ("mDNS heard
    12s ago, ping 47s ago, MQTT 2 min ago, RTT 4 ms") would
    bloat the broadcast ``subscribe_events`` channel for every
    other connected client; this stream stays scoped to the
    drawer's lifetime. Spawns the mDNS A-record refresh loop
    alongside so the displayed age doesn't grow past the
    cached TTL while the user watches.
    """
    if client is None:
        return
    if not device_name:
        raise CommandError(ErrorCode.INVALID_MESSAGE, "device_name is required")
    if controller.get_reachability_snapshot(device_name) is None:
        raise CommandError(ErrorCode.NOT_FOUND, f"No configured device named {device_name!r}")

    # Register so a peer ``devices/stop_stream`` (or this client's
    # cleanup on disconnect) cancels the running task.
    with registered_stream(client, message_id):
        refresh_task: asyncio.Task | None = None

        async def _send_initial(controls: StreamControls) -> None:
            snapshot = controller.get_reachability_snapshot(device_name)
            if snapshot is not None:
                await client.send_event(message_id, "reachability_state", snapshot)
            await client.send_result(message_id, {"subscribed": True})

        def _handle_event(event: Event[DeviceReachabilityData], controls: StreamControls) -> None:
            data = event.data
            if data["device"] != device_name:
                # Bus event is broadcast to all subscribers; filter
                # so each only forwards its own device's events.
                return
            controls.push("reachability_state", data)

        try:
            # Routed through the controller's bound delegate so tests
            # patching the loop on the instance still intercept.
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


async def refresh_loop(controller: DevicesController, device_name: str) -> None:
    """
    Schedule mDNS refreshes off the cached A record's expiry.

    Quiet on ping (regular sweep covers it) and MQTT
    (discover-publish loop ticks every 2s). Scheduled-on-expiry
    rather than fixed-interval because ``async_resolve_host``
    short-circuits on cache hits, so a fixed-interval probe
    inside the cache lifetime never reaches the wire.
    ``ServiceBrowser`` only refreshes the PTR record (4500s
    TTL); A/AAAA decay at 120s without this loop.
    """
    while True:
        # Use the A/AAAA-specific TTL; the union-of-types
        # ``get_mdns_cache_info`` includes PTR's 4500s TTL and
        # would never wake up to refresh A.
        a_ttl_remaining = controller._state_monitor.get_mdns_a_record_ttl_remaining(device_name)
        if a_ttl_remaining is not None and a_ttl_remaining > 0:
            # A still alive; sleep until just past expiry, then
            # re-check (an unrelated announce during the sleep
            # would re-arm the cache and the recheck spares us
            # a redundant wire query).
            await asyncio.sleep(a_ttl_remaining + _MDNS_REFRESH_PADDING_SECONDS)
            continue
        # A expired or absent; padding before the first probe
        # gives the subscription's initial snapshot a chance
        # to land before we issue the first query.
        await asyncio.sleep(_MDNS_REFRESH_PADDING_SECONDS)
        if controller._state_monitor.priority_for(device_name) is ReachabilitySource.MDNS:
            await controller.refresh_device_mdns(device_name)


def build_snapshot(controller: DevicesController, name: str) -> DeviceReachabilityData | None:
    """
    Stitch state + tracker fields into the reachability wire shape.

    The state monitor owns ``state`` / ``active_source`` /
    ``ip``; the tracker owns the per-signal freshness fields.
    Returns ``None`` when no configured device matches *name*.
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

    Fires :data:`EventType.DEVICE_REACHABILITY`; the drawer's
    per-device subscription filters by ``data["device"]`` and
    pushes the snapshot. Not forwarded by the broadcast
    ``subscribe_events`` channel since a per-device freshness
    ping to every connected client would bloat the bus for no
    UI gain.
    """
    snapshot = controller._build_reachability_snapshot(name)
    if snapshot is None:
        return
    controller._db.bus.fire(EventType.DEVICE_REACHABILITY, snapshot)


async def refresh_device_mdns(controller: DevicesController, name: str) -> None:
    """Force-refresh a device's mDNS A record. No-op if zeroconf is down."""
    await controller._state_monitor.refresh_mdns(name)
