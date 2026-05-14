"""ICMP ping fallback source for the device-state monitor."""

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

_PING_INTERVAL = 60  # seconds between ping sweeps
# Bootstrap delay gives the mDNS browser a head start so the
# common case (everything announces) skips a redundant ping the
# browser would have flipped ONLINE for free. 10s mirrors the
# upstream dashboard's ``MDNS_BOOTSTRAP_TIME``.
_PING_BOOTSTRAP_DELAY = 10
# icmplib gets unreliable past a few dozen concurrent probes;
# 24 matches the upstream ``GROUP_SIZE`` and keeps each batch
# inside a single ICMP timeout window.
_PING_BATCH_SIZE = 24


class PingSource:
    """ICMP ping loop owning the periodic sweep and per-device probe."""

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor
        # ``probe_device`` short-circuits while False so the
        # cold-start scanner-ADDED storm doesn't fan out a
        # full-fleet ping during the mDNS bootstrap window. Flipped
        # True after the bootstrap sleep.
        self._bootstrap_complete = False
        # Shared across the periodic sweep and eager per-device
        # probes so the combined concurrent-ICMP load can't exceed
        # icmplib's reliability ceiling (`_PING_BATCH_SIZE`) when
        # both paths fire at once.
        self._concurrency = asyncio.Semaphore(_PING_BATCH_SIZE)

    async def run(self) -> None:
        await asyncio.sleep(_PING_BOOTSTRAP_DELAY)
        self._bootstrap_complete = True
        # Strict pause when wired to a SubscriberPresence gate: only
        # sweep while at least one dashboard client is subscribed,
        # so a quiet network with no observers generates no ICMP
        # traffic. The 0→1 transition wakes the loop immediately
        # via ``wait_for_subscriber`` — mDNS keeps running
        # unconditionally because it's passive.
        monitor = self._monitor
        while True:
            if monitor._presence is not None:
                await monitor._presence.wait_for_subscriber()
            await shared.resolve_non_api_mdns_targets(monitor)
            await self._ping_sweep()
            if monitor._presence is not None:
                # Interruptible idle wait: bail early when the last
                # subscriber leaves so the next one to connect
                # doesn't sit through the rest of a stale interval.
                # ``wait_for`` raises ``TimeoutError`` after
                # ``_PING_INTERVAL`` on the still-subscribed path;
                # either branch loops back to the gate at the top.
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
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Pinging %d devices: %s",
                len(devices_to_ping),
                ", ".join(f"{d.name} ({d.address})" for d in devices_to_ping),
            )
        # Single ``gather`` plus ``self._concurrency`` semaphore
        # caps in-flight ICMP at ``_PING_BATCH_SIZE`` across the
        # sweep and any concurrent eager probes; no need to
        # pre-chunk here.
        await asyncio.gather(
            *(self._resolve_and_ping(device) for device in devices_to_ping),
            return_exceptions=True,
        )

    def _select_ping_targets(self) -> list[Device]:
        """Filter the device list down to actual ping candidates."""
        return [d for d in self._monitor._get_devices() if self._classify_for_ping(d)]

    def _classify_for_ping(self, device: Device) -> bool:
        """
        Return True iff *device* needs an ICMP probe; apply any short-circuit side-effects.

        Three filters apply: skip when a higher-priority source
        owns the device; claim ``.local`` cache hits for mDNS so
        the bare-hostname DNS fallback can't resolve them off-
        subnet; flip OFFLINE without probing when DNS already
        failed (no point hammering the resolver).
        """
        monitor = self._monitor
        if not device.address or not shared.should_ping(monitor, device):
            return False
        if is_local_hostname(device.address) and (
            cached := monitor.get_cached_addresses(device.address)
        ):
            monitor.apply(device.name, DeviceState.ONLINE, "mdns", claim=True)
            # Forward every cached IP so the dashboard shows all
            # of them; ``apply_ip_addresses`` picks the IPv4
            # primary for ICMP / OTA targeting.
            monitor.apply_ip_addresses(device.name, cached)
            return False
        if monitor.state.dns_cache.has_cached_failure(device.address):
            # DNS-failure cache entry: don't hand the bare hostname
            # to icmplib (it would hammer the system resolver every
            # sweep). Apply OFFLINE under the ``ping`` source so a
            # future successful resolve can flip the device back.
            _LOGGER.debug(
                "Skipping ping for %s (%s): cached DNS failure", device.name, device.address
            )
            monitor.apply(device.name, DeviceState.OFFLINE, "ping")
            return False
        return True

    async def _resolve_and_ping(self, device: Device) -> None:
        """Resolve *device.address* through the DNS cache and ICMP it."""
        monitor = self._monitor
        async with self._concurrency:
            addresses = await monitor.state.dns_cache.async_resolve(device.address)
            if not addresses:
                monitor.apply(device.name, DeviceState.OFFLINE, "ping")
                return
            target = addresses[0]
            # ``apply_ip`` is the only path that populates
            # ``device.ip`` for ``.local`` hosts that don't broadcast
            # ``_esphomelib._tcp`` (non-API ESPHome devices); without
            # it those devices would show an em-dash in the drawer's
            # IP row even after successful pings.
            monitor.apply_ip(device.name, target)
            await self._ping_device(device, target)

    async def _ping_device(self, device: Device, target: str) -> None:
        # Any failure mode flips OFFLINE rather than staying
        # UNKNOWN — ``NameLookupError``, ``NoRouteToHost``,
        # ``PermissionError``, socket-open failures all mean
        # "we tried and couldn't reach this". A subsequent
        # successful ping flips it back to ONLINE.
        monitor = self._monitor
        rtt_ms: float | None = None
        try:
            result = await icmp_ping(target, count=1, timeout=3, privileged=False)
            is_alive = result.is_alive
            # ``Host.min_rtt`` is 0.0 on a failed ping which would
            # surface as "0 ms" in the drawer — gate the capture
            # on ``is_alive`` so failures stay null instead.
            if is_alive:
                rtt_ms = float(result.min_rtt)
        except (ICMPLibError, OSError) as exc:
            # ``.local`` hosts on systems without Avahi / mdnsd
            # hit this every sweep; one-line debug avoids
            # flooding the logs with stack traces.
            _LOGGER.debug("Ping of %s (%s) failed: %s", device.name, target, exc)
            is_alive = False
        new_state = DeviceState.ONLINE if is_alive else DeviceState.OFFLINE
        if is_alive and rtt_ms is not None and monitor.state.reachability is not None:
            monitor.state.reachability.record_ping_rtt(device.name, rtt_ms)
        monitor.apply(device.name, new_state, "ping")

    async def probe_device(self, name: str) -> None:
        """Eagerly ICMP-probe *name* instead of waiting on the next periodic sweep."""
        if not self._bootstrap_complete or icmp_ping is None:
            return
        bucket = self._monitor._get_devices_by_name(name)
        if not bucket or not self._classify_for_ping(bucket[0]):
            return
        await self._resolve_and_ping(bucket[0])
