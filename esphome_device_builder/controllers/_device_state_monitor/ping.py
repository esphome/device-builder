"""ICMP ping fallback source for the device-state monitor."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from icmplib import async_ping as icmp_ping
from icmplib.exceptions import ICMPLibError

from ...helpers.hostname import is_local_hostname
from ...models import Device, DeviceState
from . import shared
from .helpers import _pick_ipv4

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


async def _can_use_icmp_lib_with_privilege() -> bool | None:
    """Probe both ICMP socket modes once; return the one that works.

    Privileged ``SOCK_RAW`` needs ``CAP_NET_RAW``; unprivileged
    ``SOCK_DGRAM`` needs ``net.ipv4.ping_group_range`` to
    include the process GID, which most container images do
    not set by default. ``None`` when neither works (sweep
    disabled, state follows mDNS only).
    """
    try:
        await icmp_ping("127.0.0.1", count=0, timeout=0, privileged=True)
    except (ICMPLibError, OSError):
        try:
            await icmp_ping("127.0.0.1", count=0, timeout=0, privileged=False)
        except (ICMPLibError, OSError):
            return None
        return False
    return True


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
        # Set in ``run`` once the privilege probe lands; the pre-
        # ``run`` default is only seen by tests that mock ``icmp_ping``.
        self._privileged: bool = True
        # 0→1 multiplexed into the same wake event so a subscriber
        # arriving mid-idle gets fresh ICMP without waiting out the
        # rest of the interval.
        if monitor._presence is not None:
            monitor._presence.add_subscriber_callback(self._wake.set)

    async def run(self) -> None:
        await asyncio.sleep(_PING_BOOTSTRAP_DELAY)
        privileged = await _can_use_icmp_lib_with_privilege()
        if privileged is None:
            _LOGGER.warning(
                "ICMP ping sweep disabled: opening an ICMP socket was denied in both "
                "privileged and unprivileged modes (needs CAP_NET_RAW, or "
                "net.ipv4.ping_group_range covering this process's group); "
                "device state will only update via mDNS"
            )
            return
        self._privileged = privileged
        _LOGGER.debug("Using icmplib in privileged=%s mode for the ICMP ping sweep", privileged)
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
            if is_local_hostname(device.address) and monitor.get_cached_addresses(device.address):
                # zeroconf has this ``.local`` host in its mDNS cache. Ping it
                # (``_resolve_and_ping`` resolves, then falls back to the cached
                # address) rather than claiming ONLINE off cache presence: a
                # stale or reflected cache entry for a dead device would
                # otherwise latch it ONLINE forever, since ``mdns`` priority
                # locks out the sweep and there is no browser ``Removed`` for a
                # cache-only claim (#1776). Routed here ahead of the DNS-failure
                # branch so a cached lookup failure can't strand a device we
                # still hold an mDNS address for.
                pingable.append(device)
                continue
            if monitor.state.dns_cache.has_cached_failure(device.address) and (
                not device.ip_addresses
            ):
                # The ``.local`` won't resolve and we have no known IP.
                # Don't hand the bare hostname to icmplib (it would hammer
                # the system resolver every sweep). Apply OFFLINE under the
                # ``ping`` source so a future successful resolve can flip
                # the device back. A device with a known IP (e.g. from MQTT)
                # falls through to ``pingable`` and is pinged at that IP.
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
            if not addresses and is_local_hostname(device.address):
                # System resolver couldn't resolve the ``.local`` (no nss-mdns
                # in most container images). Fall back to zeroconf's own mDNS
                # cache, kept fresh by the ``AsyncServiceBrowser``, rather than
                # giving up — but ping still decides liveness, so a stale or
                # reflected entry demotes instead of latching ONLINE (#1776).
                addresses = monitor.get_cached_addresses(device.address)
            if not addresses:
                # mDNS-less devices: the ``.local`` won't resolve but a
                # prior MQTT/DNS observation left a usable IP. Ping that so
                # ping can confirm a device the network won't resolve.
                addresses = list(device.ip_addresses)
            if not addresses:
                monitor.apply(device.name, DeviceState.OFFLINE, "ping")
                return
            # Ping the IPv4 primary, not ``addresses[0]`` — a resolve/cache
            # hit can order a scoped IPv6 first even when an IPv4 is present,
            # and ICMP across subnets is friendlier on V4. ``_pick_ipv4`` is
            # the same chooser ``apply_ip_addresses`` uses for ``device.ip``,
            # so the pinged IP and the drawer's primary stay in lockstep.
            target = _pick_ipv4(addresses)
            # ``apply_ip_addresses`` populates ``device.ip`` (V4 primary)
            # and the full ``device.ip_addresses`` list for ``.local`` hosts
            # that don't broadcast ``_esphomelib._tcp`` (non-API ESPHome
            # devices); without it those devices show an em-dash in the
            # drawer's IP row even after successful pings, and forwarding the
            # whole set keeps a cached multi-IP device's secondary addresses.
            monitor.apply_ip_addresses(device.name, addresses)
            await self._ping_device(device, target)

    async def _ping_device(self, device: Device, target: str) -> None:
        # Any failure mode flips OFFLINE rather than staying
        # UNKNOWN — ``NameLookupError``, ``NoRouteToHost``,
        # ``PermissionError``, socket-open failures all mean
        # "we tried and couldn't reach this". A subsequent
        # successful ping flips it back to ONLINE.
        monitor = self._monitor
        rtt_ms: float | None = None
        # Skip the retry only for already-OFFLINE devices: the miss
        # just confirms the state, nothing to flap. ONLINE devices
        # get the retry to absorb a transient drop; UNKNOWN devices
        # get it too so the first classification on a lossy path
        # (dashboard cold-start, every device starts UNKNOWN) doesn't
        # immediately label a reachable device OFFLINE on a single
        # dropped packet.
        needs_retry = device.state is not DeviceState.OFFLINE
        privileged = self._privileged
        try:
            result = await icmp_ping(target, count=1, timeout=3, privileged=privileged)
            is_alive = result.is_alive
            if not is_alive and needs_retry:
                # Retry with multiple packets before flapping the
                # indicator. A single dropped ICMP would otherwise
                # flap on lossy paths (VPN, congested Wi-Fi).
                result = await icmp_ping(
                    target, count=3, interval=0.5, timeout=2, privileged=privileged
                )
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
