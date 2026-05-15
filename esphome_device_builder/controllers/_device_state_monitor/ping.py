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


def _format_devices(devices: list[Device]) -> str:
    """Render *devices* as ``"name (address), …"`` for log messages."""
    return ", ".join(f"{d.name} ({d.address})" for d in devices)


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
    """ICMP ping loop owning the periodic sweep and the wake-on-add early trigger."""

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor
        # Cleared at the top of each sweep so a wake fired mid-sweep
        # still triggers the next idle.
        self._wake = asyncio.Event()
        self._concurrency = asyncio.Semaphore(_PING_BATCH_SIZE)
        # Sorted ``(name, address)`` of every device in the union of
        # the last DEBUG sweep's pingable + dns_failed buckets. Spanning
        # both keeps the signature stable across the DNS-failure cache
        # flicker (120s TTL vs 60s sweep) so the line only re-emits on
        # real membership change.
        self._last_logged_targets: tuple[tuple[str, str], ...] = ()
        # 0→1 multiplexed into the same wake event so a subscriber
        # arriving mid-idle gets fresh ICMP without waiting out the
        # rest of the interval.
        if monitor._presence is not None:
            monitor._presence.add_subscriber_callback(self._wake.set)

    async def run(self) -> None:
        await asyncio.sleep(_PING_BOOTSTRAP_DELAY)
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
            self._wake.clear()
            await shared.resolve_non_api_mdns_targets(monitor)
            await self._ping_sweep()
            await self._idle()

    def wake(self) -> None:
        """Bail the idle wait so the next sweep runs without waiting on ``_PING_INTERVAL``."""
        self._wake.set()

    async def _idle(self) -> None:
        """Sleep up to ``_PING_INTERVAL`` or until the wake event fires."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=_PING_INTERVAL)

    async def _ping_sweep(self) -> None:
        if icmp_ping is None:
            return
        pingable, dns_failed = self._select_ping_targets()
        if not pingable and not dns_failed:
            return
        if _LOGGER.isEnabledFor(logging.DEBUG):
            # Signature spans both buckets so a device whose 120s
            # DNS-failure cache TTL flips it between ``pingable`` and
            # ``dns_failed`` every 60s sweep doesn't re-emit the log
            # line each cycle. New devices, mDNS claims, and removals
            # still resurface it.
            signature = tuple(sorted((d.name, d.address) for d in pingable + dns_failed))
            if signature != self._last_logged_targets:
                self._last_logged_targets = signature
                if dns_failed:
                    _LOGGER.debug(
                        "Pinging %d devices: %s; skipping %d (cached DNS failure): %s",
                        len(pingable),
                        _format_devices(pingable) or "(none)",
                        len(dns_failed),
                        _format_devices(dns_failed),
                    )
                else:
                    _LOGGER.debug(
                        "Pinging %d devices: %s", len(pingable), _format_devices(pingable)
                    )
        # ``self._concurrency`` semaphore caps in-flight ICMP at
        # ``_PING_BATCH_SIZE``; no need to pre-chunk the gather.
        await asyncio.gather(
            *(self._resolve_and_ping(device) for device in pingable),
            return_exceptions=True,
        )

    def _select_ping_targets(self) -> tuple[list[Device], list[Device]]:
        """Return ``(pingable, dns_failed)`` and apply per-device side-effects."""
        pingable: list[Device] = []
        dns_failed: list[Device] = []
        monitor = self._monitor
        for device in monitor._get_devices():
            if not device.address or not shared.should_ping(monitor, device):
                continue
            if is_local_hostname(device.address) and (
                cached := monitor.get_cached_addresses(device.address)
            ):
                monitor.apply(device.name, DeviceState.ONLINE, "mdns", claim=True)
                # Forward every cached IP so the dashboard shows all
                # of them; ``apply_ip_addresses`` picks the IPv4
                # primary for ICMP / OTA targeting.
                monitor.apply_ip_addresses(device.name, cached)
                continue
            if monitor.state.dns_cache.has_cached_failure(device.address):
                # Don't hand the bare hostname to icmplib (it would
                # hammer the system resolver every sweep). Apply
                # OFFLINE under the ``ping`` source so a future
                # successful resolve can flip the device back.
                monitor.apply(device.name, DeviceState.OFFLINE, "ping")
                dns_failed.append(device)
                continue
            pingable.append(device)
        return pingable, dns_failed

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
