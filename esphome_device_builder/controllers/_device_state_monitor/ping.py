"""ICMP ping fallback source for the device-state monitor."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from icmplib import async_ping as icmp_ping
from icmplib.exceptions import ICMPLibError

from ...helpers.hostname import is_local_hostname
from ...models import Device, DeviceState
from . import shared
from ._sweep_source import SweepSource
from .helpers import _pick_ipv4

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)


def _format_devices(devices: list[Device]) -> str:
    """Render *devices* as ``"name (address), …"`` for log messages."""
    return ", ".join(f"{d.name} ({d.address})" for d in devices)


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


class PingSource(SweepSource):
    """ICMP ping loop owning the periodic sweep and the wake-on-add early trigger."""

    _sweep_label = "ICMP ping"
    # The mDNS browser's head start so the common case (everything
    # announces) skips a redundant ping the browser would have flipped
    # ONLINE for free. 10s mirrors the upstream dashboard's
    # ``MDNS_BOOTSTRAP_TIME``.
    _bootstrap_delay = 10

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        super().__init__(monitor)
        # One in-flight budget for every ICMP consumer — the sweep and
        # the reviver's pre-filter share it so overlapping loops can't
        # exceed the icmplib reliability bound together.
        self.icmp_concurrency = asyncio.Semaphore(shared.ICMP_BATCH_SIZE)
        # Sorted ``(name, address)`` of every device in the union of
        # the last DEBUG sweep's pingable + dns_failed buckets. Spanning
        # both keeps the signature stable across the DNS-failure cache
        # flicker (120s TTL vs 60s sweep) so the line only re-emits on
        # real membership change.
        self._last_logged_targets: tuple[tuple[str, str], ...] = ()
        # Set in ``_prepare`` once the privilege probe lands; the pre-
        # ``run`` default is only seen by tests that mock ``icmp_ping``.
        self._privileged: bool = True
        # Privilege-probe outcome for siblings (the API reviver gates on
        # it): ``None`` until the probe lands, then True iff some ICMP
        # socket mode works and the sweep is running.
        self.icmp_available: bool | None = None

    def wake(self) -> None:
        """Bail the idle wait so the next sweep runs without waiting out the interval."""
        self._wake.set()

    async def _prepare(self) -> bool:
        privileged = await _can_use_icmp_lib_with_privilege()
        self.icmp_available = privileged is not None
        if privileged is None:
            _LOGGER.warning(
                "ICMP ping sweep disabled: opening an ICMP socket was denied in both "
                "privileged and unprivileged modes (needs CAP_NET_RAW, or "
                "net.ipv4.ping_group_range covering this process's group); "
                "device state will only update via mDNS"
            )
            return False
        self._privileged = privileged
        _LOGGER.debug("Using icmplib in privileged=%s mode for the ICMP ping sweep", privileged)
        return True

    async def _sweep(self) -> None:
        # Disjoint candidate sets — resolve both concurrently so a
        # wire-miss in one doesn't delay the sweep behind the other.
        # A failing resolve step is logged and must not skip the
        # sibling resolve or the ping pass for this interval.
        results = await asyncio.gather(
            shared.resolve_non_api_mdns_targets(self._monitor),
            shared.resolve_api_mdns_targets(self._monitor),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                # Never mask a cancellation as a benign step failure.
                raise result
            if isinstance(result, Exception):
                _LOGGER.warning("mDNS resolve step failed; continuing", exc_info=result)
        await self._ping_sweep()

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
        # ``self.icmp_concurrency`` semaphore caps in-flight ICMP at
        # ``ICMP_BATCH_SIZE``; no need to pre-chunk the gather.
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
            if shared.address_resolution_exhausted(monitor, device.address) and (
                not device.runtime_state.ip_addresses
            ):
                # The address won't resolve and we have no known IP.
                # Don't hand the bare hostname to icmplib (it would hammer
                # the system resolver every sweep). Apply OFFLINE under the
                # ``ping`` source so a future successful resolve can flip
                # the device back. Everything else falls through to
                # ``pingable``: a zeroconf-cached ``.local`` gets *pinged*
                # rather than claimed ONLINE off cache presence (a stale or
                # reflected entry for a dead device would latch it ONLINE
                # forever, #1776), and a device with a known IP (e.g. from
                # MQTT) is pinged at that IP. One predicate, shared with
                # the reviver's cohort gate.
                monitor.apply(device.name, DeviceState.OFFLINE, "ping")
                dns_failed.append(device)
                continue
            pingable.append(device)
        return pingable, dns_failed

    async def _resolve_and_ping(self, device: Device) -> None:
        """Resolve *device.address* through the DNS cache and ICMP it."""
        monitor = self._monitor
        async with self.icmp_concurrency:
            addresses = await monitor.state.dns_cache.async_resolve(device.address)
            if not addresses and is_local_hostname(device.address):
                # System resolver couldn't resolve the ``.local`` (no nss-mdns
                # in most container images). Fall back to zeroconf's own mDNS
                # cache, kept fresh by the ``AsyncServiceBrowser``, rather than
                # giving up — but ping still decides liveness, so a stale or
                # reflected entry demotes instead of latching ONLINE (#1776).
                addresses = monitor.mdns.get_cached_addresses(device.address)
            if not addresses:
                # mDNS-less devices: the ``.local`` won't resolve but a
                # prior MQTT/DNS observation left a usable IP. Ping that so
                # ping can confirm a device the network won't resolve.
                addresses = list(device.runtime_state.ip_addresses)
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
            # and the full ``runtime_state.ip_addresses`` list for ``.local`` hosts
            # that don't broadcast ``_esphomelib._tcp`` (non-API ESPHome
            # devices); without it those devices show an em-dash in the
            # drawer's IP row even after successful pings, and forwarding the
            # whole set keeps a cached multi-IP device's secondary addresses.
            monitor.apply_ip_addresses(device.name, addresses)
            await self._ping_device(device, target)

    async def ping_once(self, target: str, *, retry: bool) -> float | None:
        """
        ICMP *target* once; RTT in ms when alive, ``None`` when unreachable.

        Any failure mode reads as unreachable — ``NameLookupError``,
        ``NoRouteToHost``, ``PermissionError``, socket-open failures
        all mean "we tried and couldn't reach this". *retry* re-probes
        a miss with multiple packets so a single dropped ICMP doesn't
        read as dead on lossy paths (VPN, congested Wi-Fi). Callers
        hold ``icmp_concurrency`` — the probe itself doesn't acquire
        it (the sweep holds one slot across resolve + ping).
        """
        privileged = self._privileged
        try:
            result = await icmp_ping(target, count=1, timeout=3, privileged=privileged)
            if not result.is_alive and retry:
                result = await icmp_ping(
                    target, count=3, interval=0.5, timeout=2, privileged=privileged
                )
            # ``Host.min_rtt`` is 0.0 on a failed ping which would
            # surface as "0 ms" in the drawer — gate the capture
            # on ``is_alive`` so failures stay null instead.
            if result.is_alive:
                return float(result.min_rtt)
        except (ICMPLibError, OSError) as exc:
            # ``.local`` hosts on systems without Avahi / mdnsd
            # hit this every sweep; one-line debug avoids
            # flooding the logs with stack traces.
            _LOGGER.debug("Ping of %s failed: %s", target, exc)
        return None

    async def _ping_device(self, device: Device, target: str) -> None:
        # Skip the retry only for already-OFFLINE devices: the miss
        # just confirms the state, nothing to flap. ONLINE devices
        # get the retry to absorb a transient drop; UNKNOWN devices
        # get it too so the first classification on a lossy path
        # (dashboard cold-start, every device starts UNKNOWN) doesn't
        # immediately label a reachable device OFFLINE on a single
        # dropped packet.
        needs_retry = device.runtime_state.state is not DeviceState.OFFLINE
        rtt_ms = await self.ping_once(target, retry=needs_retry)
        shared.apply_ping_result(self._monitor, device.name, rtt_ms)
