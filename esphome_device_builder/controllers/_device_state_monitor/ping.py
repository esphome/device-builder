"""
ICMP ping fallback source for the device-state monitor.

Mirrors the legacy ``esphome/dashboard/status/ping.py`` shape:
a :class:`PingSource` taking the monitor in ``__init__``, owning
the periodic sweep, target selection, and per-device probe.
Reads from and writes back through ``monitor.state`` /
``monitor.apply(...)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ...helpers.hostname import is_local_hostname
from ...models import Device, DeviceState
from . import shared

try:
    from icmplib import async_ping as icmp_ping
    from icmplib.exceptions import ICMPLibError
except ImportError:  # pragma: no cover — icmplib is optional
    icmp_ping = None  # type: ignore[assignment]
    ICMPLibError = Exception  # type: ignore[misc,assignment]

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)

# Ping fallback runs every 60s after a short bootstrap window.
# ``_PING_BOOTSTRAP_DELAY`` gives the mDNS browser a head start so the
# common case (everything announces) doesn't fire a ping sweep that
# the browser would have answered for free a few seconds later. 10s
# tracks the upstream esphome dashboard's ``MDNS_BOOTSTRAP_TIME``
# (~7.5s) closely enough to stay correct without making the user wait
# a full minute to see UNKNOWN devices flip OFFLINE on first load.
_PING_INTERVAL = 60  # seconds between ping sweeps
_PING_BOOTSTRAP_DELAY = 10  # seconds before the first ping sweep
# Batch size matches the upstream esphome dashboard's
# ``GROUP_SIZE = MAX_EXECUTOR_WORKERS / 2 = 24``. Each batch's pings
# run in parallel via ``asyncio.gather``; the cap exists because
# icmplib gets unreliable past a few dozen concurrent probes. With a
# small fleet (≤24 ping candidates) one batch covers everything and
# the sweep finishes in a single ICMP timeout window instead of
# stacking N timeouts back-to-back.
_PING_BATCH_SIZE = 24


class PingSource:
    """
    ICMP ping loop owning the periodic sweep and per-device probe.

    Takes the monitor in ``__init__`` and reads / writes through it
    (``monitor.state.dns_cache``, ``monitor.apply(...)``, etc.).
    The shared cross-cutting bits — should_ping precedence,
    apply_resolved_addresses funnel, the active mDNS resolve path
    that primes ONLINE before the sweep — live in
    :mod:`._device_state_monitor.shared`.
    """

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor

    async def run(self) -> None:
        # First sweep after the short bootstrap window — gives mDNS a
        # head start so we don't redundantly ping devices the browser
        # is about to flip ONLINE for free, but still gets the UNKNOWN
        # → OFFLINE transition in front of the user within ~10s of
        # startup instead of after a full minute.
        await asyncio.sleep(_PING_BOOTSTRAP_DELAY)
        # Strict pause: when wired to a SubscriberPresence gate, only
        # sweep while at least one dashboard client is subscribed.
        # The 0→1 transition wakes ``wait_for_subscriber`` immediately
        # so the first user to open the dashboard sees fresh ICMP-
        # source state within one sweep instead of waiting up to
        # ``_PING_INTERVAL``. mDNS browsing keeps running
        # unconditionally (it's passive), so devices that announce
        # flip ONLINE the moment the bus delivers their cached state
        # to the new subscriber.
        monitor = self._monitor
        while True:
            if monitor._presence is not None:
                await monitor._presence.wait_for_subscriber()
            await shared.resolve_non_api_mdns_targets(monitor)
            await self._ping_sweep()
            if monitor._presence is not None:
                # Interruptible idle wait: bail early if the last
                # subscriber leaves so the next one to connect
                # doesn't sit through the rest of a stale interval.
                # ``wait_for`` raises ``TimeoutError`` after
                # ``_PING_INTERVAL`` when the gate stays open the
                # whole time (the normal "still subscribed, sweep
                # again" path). Either branch loops back to the top
                # where ``wait_for_subscriber`` parks if the gate
                # has since closed.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        monitor._presence.wait_for_no_subscribers(),
                        timeout=_PING_INTERVAL,
                    )
                continue
            await asyncio.sleep(_PING_INTERVAL)

    async def _ping_sweep(self) -> None:
        if icmp_ping is None:
            return

        devices_to_ping = self._select_ping_targets()
        if not devices_to_ping:
            return

        monitor = self._monitor
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Pinging %d devices: %s",
                len(devices_to_ping),
                ", ".join(f"{d.name} ({d.address})" for d in devices_to_ping),
            )

        for i in range(0, len(devices_to_ping), _PING_BATCH_SIZE):
            batch = devices_to_ping[i : i + _PING_BATCH_SIZE]
            # Pre-resolve every batch via the DNS cache. icmplib would
            # otherwise re-resolve internally on every ping (going to
            # the system resolver each time and ignoring our cache),
            # and the OTA cache args would have nothing to draw on for
            # non-mDNS hostnames.
            resolved = await asyncio.gather(
                *(monitor.state.dns_cache.async_resolve(d.address) for d in batch),
                return_exceptions=True,
            )
            ping_targets: list[tuple[Device, str]] = []
            for device, addresses in zip(batch, resolved, strict=True):
                if isinstance(addresses, list) and addresses:
                    target = addresses[0]
                    # Apply the resolved target so the drawer / table
                    # have an IP to show. ``apply_ip`` already
                    # preserves an existing multi-IP set when the
                    # incoming target is already in it (the typical
                    # case for a ``.local`` host with an active
                    # ``_esphomelib._tcp`` broadcast — the ping
                    # target is the IPv4 primary the browser
                    # callback already populated). For ``.local``
                    # hosts that don't broadcast ``_esphomelib._tcp``
                    # (non-API ESPHome devices, the
                    # zwave-proxy-seeedw5500 case) this is the only
                    # path that ever populates ``device.ip``, so a
                    # ping-source-only device would otherwise show
                    # an em-dash in the drawer's IP row even after
                    # successful pings.
                    monitor.apply_ip(device.name, target)
                    ping_targets.append((device, target))
                else:
                    # DNS cache says we can't resolve this hostname
                    # (the entry is cached as a failure for the cache
                    # TTL). Don't hand the bare hostname to icmplib —
                    # it would re-resolve via the system resolver every
                    # sweep, hammering DNS for nothing. Treat the cache
                    # miss as the "we tried, can't reach" signal and
                    # apply OFFLINE via the same source ``_ping_device``
                    # would have used.
                    monitor.apply(device.name, DeviceState.OFFLINE, "ping")
            if ping_targets:
                await asyncio.gather(
                    *(self._ping_device(device, target) for device, target in ping_targets),
                    return_exceptions=True,
                )

    def _select_ping_targets(self) -> list[Device]:
        """
        Filter the device list down to actual ping candidates.

        Devices already known to be ONLINE via a higher-priority source
        are skipped. ``.local`` hosts that show up in zeroconf's cache
        are claimed for mDNS so the bare-hostname DNS fallback can't
        resolve them to an unreachable IP on a different subnet.
        Hostnames with a fresh DNS-failure cache entry are flipped
        OFFLINE without a ping attempt — there's nothing to resolve, so
        re-trying every minute would just hammer the resolver.
        """
        monitor = self._monitor
        devices_to_ping: list[Device] = []
        dns_skipped: list[Device] = []
        for device in monitor._get_devices():
            if not device.address or not shared.should_ping(monitor, device):
                continue
            if is_local_hostname(device.address) and (
                cached := monitor.get_cached_addresses(device.address)
            ):
                monitor.apply(device.name, DeviceState.ONLINE, "mdns", claim=True)
                # Forward every cached IP so the dashboard shows all
                # of them; ``apply_ip_addresses`` picks an IPv4 primary
                # for ``device.ip`` so ICMP probes and OTA cache args
                # still hit the cross-subnet-friendly entry.
                monitor.apply_ip_addresses(device.name, cached)
                continue
            if monitor.state.dns_cache.has_cached_failure(device.address):
                dns_skipped.append(device)
                monitor.apply(device.name, DeviceState.OFFLINE, "ping")
                continue
            devices_to_ping.append(device)

        if dns_skipped and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Skipping ping for %d device(s) with cached DNS failure: %s",
                len(dns_skipped),
                ", ".join(f"{d.name} ({d.address})" for d in dns_skipped),
            )
        return devices_to_ping

    async def _ping_device(self, device: Device, target: str) -> None:
        # Treat any failure mode as "not reachable" → OFFLINE, not as
        # "still unknown". An exception here means resolution failed
        # (NameLookupError), the network refused us (NoRouteToHost,
        # PermissionError, OSError), or icmplib couldn't open a socket.
        # In every case the user wants the dot to flip red, not stay
        # grey forever — once mDNS / MQTT / ping have all tried, the
        # signal is "we couldn't reach this device". A subsequent
        # successful ping will flip it right back to ONLINE.
        monitor = self._monitor
        rtt_ms: float | None = None
        try:
            result = await icmp_ping(target, count=1, timeout=3, privileged=False)
            is_alive = result.is_alive
            # icmplib's ``Host.min_rtt`` is the lowest round-trip in
            # milliseconds across the count we sent (1 here). Capture
            # it before discarding ``result`` so the drawer can show
            # "4 ms" beside the Ping row. ``min_rtt`` is 0.0 on a
            # failed ping which would surface as "0 ms" — gate on
            # ``is_alive`` so failures stay null.
            if is_alive:
                rtt_ms = float(result.min_rtt)
        except (ICMPLibError, OSError) as exc:
            # ``.local`` hosts on systems without Avahi / mdnsd hit
            # this every sweep; the traceback adds nothing and floods
            # the logs. One-line debug is plenty.
            _LOGGER.debug("Ping of %s (%s) failed: %s", device.name, target, exc)
            is_alive = False
        new_state = DeviceState.ONLINE if is_alive else DeviceState.OFFLINE
        if is_alive and rtt_ms is not None and monitor.state.reachability is not None:
            monitor.state.reachability.record_ping_rtt(device.name, rtt_ms)
        monitor.apply(device.name, new_state, "ping")
