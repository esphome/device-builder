"""
Last-resort revival of stuck-offline API devices from the persisted IP.

A device whose mDNS goes dark self-heals while the process stays up (the
ping sweep keeps its RAM ``ip_addresses`` alive), but after a restart RAM
is empty, the ``.local`` won't resolve, and the sweep claims OFFLINE with
no target forever — even though the last-known IPv4 is persisted on disk
(``Device.ip``, kept for the OTA cache). A bare ICMP reply at that
wall-clock-old DHCP address is inadmissible as ONLINE evidence (whatever
now holds the lease answers, the #1776 latch class), and Native API
connects are heavy on the device (scarce connection slots), so revival is
strictly last-resort and identity-verified:

1. Candidates are devices with **no other reachability signal**: not
   ONLINE, ``api_enabled``, persisted ``Device.ip``, no RAM addresses, no
   zeroconf-cached addresses, and the address has a cached DNS failure
   (proving the ping sweep already tried and had no target).
2. ICMP the persisted IP as a cheap **negative** filter — silence means
   no dial, the device is off or moved.
3. Something answered: pay for one short-lived ``device_info`` worker
   dial. Name match (MAC corroborating when both sides know it) →
   reseed ``ip_addresses`` and claim ONLINE under the ``ping`` source,
   which owns liveness from then on. Name mismatch → the persisted IP is
   proven stale and is invalidated so nothing trusts it again.

A process-lifetime verified ``name → ip`` cache caps the cost at one dial
per stuck device per process: later flaps revive on the ICMP filter alone,
the same trust RAM-learned addresses already get. Coverage is deliberately
``api:`` devices only — MQTT devices revive via the broker, and
web_server-only / OTA-only devices have no strong identity channel.
Deployments where ICMP is unavailable are not repaired: the negative
filter can't run, and a verify-only ONLINE would be un-demotable.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import time
from typing import TYPE_CHECKING, Any

from ...helpers.hostname import is_local_hostname
from ...models import Device, DeviceState
from ._api_probe import build_probe_request, run_worker
from .helpers import _normalize_mac
from .ping import _PING_BATCH_SIZE

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)

_INTERVAL = 60  # seconds between revival sweeps
# After ping's 10s bootstrap plus a sweep, so the DNS-failure cache the
# cohort gate reads is populated and the mDNS browser had its head start.
_BOOTSTRAP_DELAY = 75
# Dials are serial and each occupies one of the device's scarce API slots;
# stricter than api_info's 8 because the post-restart stuck fleet is the
# storm case here.
_MAX_DIALS_PER_SWEEP = 3
# ICMP silence: the device is off or moved — cheap to re-check.
_ICMP_SILENT_COOLDOWN = 600.0  # seconds
# Something answers but the dial fails (refused / handshake / timeout /
# MAC conflict): likely a stranger holding the lease, so back off hard —
# doubling per consecutive failure up to the cap.
_DIAL_FAILURE_COOLDOWN = 1800.0  # seconds
_DIAL_FAILURE_COOLDOWN_MAX = 21600.0  # seconds
# Nothing changes until the YAML does.
_NO_KEY_COOLDOWN = 3600.0  # seconds


class ApiReviverSource:
    """Identity-verified last-resort ONLINE revival from the persisted IP."""

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor
        self._wake = asyncio.Event()
        self._concurrency = asyncio.Semaphore(_PING_BATCH_SIZE)
        # (name, persisted ip) -> monotonic deadline; keying on the pair
        # means a persisted-IP change bypasses the old entry naturally.
        self._cooldown: dict[tuple[str, str], float] = {}
        # (name, persisted ip) -> consecutive dial failures, for the
        # escalating backoff.
        self._dial_failures: dict[tuple[str, str], int] = {}
        # name -> IP whose responder passed identity verification this
        # process; revivals at the same pair skip the dial entirely.
        self._verified: dict[str, str] = {}
        if monitor._presence is not None:
            monitor._presence.add_subscriber_callback(self._wake.set)

    async def run(self) -> None:
        # ``find_spec`` resolves without importing, so ``aioesphomeapi``
        # never loads into the dashboard process. Unlike api_info there
        # is no lib-less work to do — identity verification IS the dial.
        if importlib.util.find_spec("aioesphomeapi") is None:
            _LOGGER.debug("aioesphomeapi not installed; API revival disabled")
            return
        await asyncio.sleep(_BOOTSTRAP_DELAY)
        monitor = self._monitor
        while True:
            if monitor._presence is not None:
                await monitor._presence.wait_for_subscriber()
            self._wake.clear()
            if monitor._ping.icmp_available is False:
                # ICMP availability can't change within a process; exit
                # for good rather than idle-spin.
                _LOGGER.warning(
                    "API revival disabled: ICMP is unavailable, so the persisted-IP "
                    "pre-filter can't run and a verified ONLINE could never demote"
                )
                return
            try:
                await self._sweep()
            except Exception:
                # A failure outside the per-device paths must not kill the
                # loop for the process lifetime; log it and keep sweeping.
                _LOGGER.exception("API reviver sweep failed; continuing")
            await self._idle()

    def wake(self) -> None:
        """Bail the idle wait so the next sweep runs without waiting on ``_INTERVAL``."""
        self._wake.set()

    async def _idle(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=_INTERVAL)

    async def _sweep(self) -> None:
        devices = self._monitor._get_devices()
        self._prune(devices)
        # ``icmp_available`` still None: the privilege probe hasn't
        # landed (it runs ~10s in, well before our bootstrap, so this is
        # a startup race at most one sweep wide).
        if self._monitor._ping.icmp_available is not True:
            return
        candidates = self._select_candidates(devices)
        if not candidates:
            return
        # Negative pre-filter: batched, cheap, and inadmissible as ONLINE
        # evidence on its own — silence means no dial this sweep.
        rtts = await asyncio.gather(*(self._prefilter(device) for device in candidates))
        answering = [
            (device, rtt) for device, rtt in zip(candidates, rtts, strict=True) if rtt is not None
        ]
        dials = 0
        for device, rtt in answering:
            if self._verified.get(device.name) == device.ip:
                # Already identity-verified at this pair this process —
                # the echo alone revives, same trust RAM addresses get.
                self._revive(device, rtt)
                continue
            if dials >= _MAX_DIALS_PER_SWEEP:
                # Un-cooled overflow rolls to the next sweep.
                continue
            dials += 1
            await self._verify_and_revive(device, rtt)

    def _select_candidates(self, devices: list[Device]) -> list[Device]:
        """Devices with a persisted IP and provably no other reachability signal."""
        now = time.monotonic()
        return [
            device
            for device in devices
            if device.api_enabled
            and device.ip
            and device.runtime_state.state is not DeviceState.ONLINE
            and not device.runtime_state.ip_addresses
            and self._cooldown.get((device.name, device.ip), 0.0) <= now
            and self._address_unresolvable(device)
        ]

    def _address_unresolvable(self, device: Device) -> bool:
        """
        Report whether the ping sweep provably has no target for *device*.

        Requiring the *cached* DNS failure (populated by the sweep's
        pre-resolve) both proves ping already tried and naturally
        sequences the reviver after at least one sweep. A ``.local``
        with zeroconf-cached addresses is ping's to handle.
        """
        address = device.address
        if not address:
            return True
        monitor = self._monitor
        if is_local_hostname(address) and monitor.get_cached_addresses(address):
            return False
        return monitor.state.dns_cache.has_cached_failure(address)

    async def _prefilter(self, device: Device) -> float | None:
        """ICMP the persisted IP; cool the pair down on silence."""
        async with self._concurrency:
            rtt = await self._monitor._ping.ping_once(device.ip)
        if rtt is None:
            self._cooldown[(device.name, device.ip)] = time.monotonic() + _ICMP_SILENT_COOLDOWN
        return rtt

    async def _verify_and_revive(self, device: Device, rtt: float) -> None:
        """One worker dial; revive on identity match, invalidate on mismatch."""
        monitor = self._monitor
        request = await build_probe_request(monitor, device, [device.ip])
        if request is None:
            self._cooldown[(device.name, device.ip)] = time.monotonic() + _NO_KEY_COOLDOWN
            return
        info = await self._run_worker(device, request)
        if info is None:
            self._record_dial_failure(device)
            return
        reported = info.get("name", "")
        if reported != device.name:
            # Whatever holds the lease now is a different device; the
            # persisted IP is proven stale — invalidate it so neither the
            # reviver nor the OTA cache trusts it again.
            _LOGGER.info(
                "Persisted IP %s for %s now answers as %r; invalidating it",
                device.ip,
                device.name,
                reported,
            )
            monitor.invalidate_persisted_ip(device.name)
            return
        mac = _normalize_mac(info.get("mac_address", ""))
        persisted_mac = _normalize_mac(device.mac_address)
        if mac and persisted_mac and mac != persisted_mac:
            _LOGGER.warning(
                "Device at %s reports name %s but MAC %s != persisted %s; not claiming ONLINE",
                device.ip,
                device.name,
                mac,
                persisted_mac,
            )
            self._record_dial_failure(device)
            return
        self._verified[device.name] = device.ip
        _LOGGER.info(
            "Revived %s at persisted IP %s (identity verified over the Native API)",
            device.name,
            device.ip,
        )
        self._revive(device, rtt, info)

    def _revive(self, device: Device, rtt: float, info: dict[str, Any] | None = None) -> None:
        """Seed the verified IP, then claim ONLINE under the ping source.

        IP before state so the first post-revival snapshot carries the
        address and the sweep has its target; the ping wake hands
        ownership of liveness to the ordinary sweep immediately.
        """
        monitor = self._monitor
        name = device.name
        monitor.apply_ip_addresses(name, [device.ip])
        if info is not None:
            monitor.apply_mac_address(name, info.get("mac_address", ""))
            monitor.apply_version(name, info.get("esphome_version", ""))
        if monitor.state.reachability is not None:
            monitor.state.reachability.record_ping_rtt(name, rtt)
        monitor.apply(name, DeviceState.ONLINE, "ping")
        monitor._ping.wake()

    async def _run_worker(self, device: Device, request: bytes) -> dict[str, Any] | None:
        """Instance seam over the shared worker runner (tests stub it here)."""
        return await run_worker(device.name, request)

    def _record_dial_failure(self, device: Device) -> None:
        """Escalating backoff for a responder that answers ICMP but fails the dial."""
        key = (device.name, device.ip)
        count = self._dial_failures.get(key, 0) + 1
        self._dial_failures[key] = count
        delay = min(_DIAL_FAILURE_COOLDOWN * 2 ** (count - 1), _DIAL_FAILURE_COOLDOWN_MAX)
        self._cooldown[key] = time.monotonic() + delay

    def _prune(self, devices: list[Device]) -> None:
        """Drop bookkeeping for gone / recovered / re-IP'd devices.

        An ONLINE transition (any source) or a persisted-IP change
        resets the escalating backoff so legitimate recovery isn't
        delayed by a stale failure streak.
        """
        current = {device.name: device for device in devices}

        def stale(key: tuple[str, str]) -> bool:
            device = current.get(key[0])
            return (
                device is None
                or device.ip != key[1]
                or device.runtime_state.state is DeviceState.ONLINE
            )

        self._cooldown = {k: v for k, v in self._cooldown.items() if not stale(k)}
        self._dial_failures = {k: v for k, v in self._dial_failures.items() if not stale(k)}
        self._verified = {n: ip for n, ip in self._verified.items() if n in current}
