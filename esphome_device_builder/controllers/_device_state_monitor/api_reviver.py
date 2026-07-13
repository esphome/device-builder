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
   ONLINE, ``api_enabled``, persisted ``Device.ip``, no RAM addresses,
   and :func:`shared.address_resolution_exhausted` proving the ping
   sweep already tried and had no target.
2. ICMP the persisted IP as a cheap **negative** filter — silence means
   no dial, the device is off or moved.
3. Something answered: pay for one short-lived ``device_info`` worker
   dial. Name match (MAC corroborating when both sides know it) →
   reseed ``ip_addresses`` and claim ONLINE under the ``ping`` source,
   which owns liveness from then on. Name mismatch → the persisted IP is
   proven stale and is invalidated so nothing trusts it again.

A verified ``name → ip`` cache lets flaps within ``_VERIFIED_TTL`` revive
on the ICMP filter alone, the same trust RAM-learned addresses already
get; past the TTL one fresh dial re-verifies, so a lease reassigned
during a long silent gap can't ride a stale verification back to ONLINE.
Coverage is deliberately ``api:`` devices only — MQTT devices revive via
the broker, and web_server-only / OTA-only devices have no strong
identity channel.
Deployments where ICMP is unavailable are not repaired: the negative
filter can't run, and a verify-only ONLINE would be un-demotable. A
confirmed mDNS ``Removed`` mid-process also clears the RAM ``Device.ip``
the cohort gate reads, so that device is only revivable after a restart
rebuilds it from the sidecar.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ...helpers.cooldown import CooldownLedger
from ...models import Device, DeviceState
from . import shared
from ._api_probe import (
    ApiSweepSource,
    ProbeError,
    api_worker_available,
    apply_worker_info,
)
from .helpers import _normalize_mac

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)

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
# How long a verified pair revives dial-free. Past this the next revival
# re-verifies identity: over a long silent gap DHCP can hand the lease to
# a stranger, and trusting a weeks-old echo would be the #1776 latch again.
_VERIFIED_TTL = 21600.0  # seconds


class ApiReviverSource(ApiSweepSource):
    """Identity-verified last-resort ONLINE revival from the persisted IP."""

    _sweep_label = "API reviver"
    # After ping's 10s bootstrap plus its privilege probe and first
    # sweep, so ``icmp_available`` is decided and the DNS-failure cache
    # the cohort gate reads is populated.
    _bootstrap_delay = 75

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        super().__init__(monitor)
        # Keyed on (name, persisted ip) so a persisted-IP change
        # bypasses the old entry naturally.
        self._cooldown: CooldownLedger[tuple[str, str]] = CooldownLedger()
        # name -> (ip, verified-at monotonic) for responders that passed
        # identity verification; a fresh pair revives without a dial.
        self._verified: dict[str, tuple[str, float]] = {}

    def _prepare(self) -> bool:
        # Unlike api_info there is no lib-less work to do — identity
        # verification IS the dial.
        if not api_worker_available():
            _LOGGER.debug("aioesphomeapi not installed; API revival disabled")
            return False
        # The bootstrap sleep outlasts ping's privilege probe, so a
        # still-undecided outcome means the probe never ran; either way
        # the negative pre-filter can't be trusted, and availability
        # can't change within a process.
        if self._monitor._ping.icmp_available is not True:
            _LOGGER.warning(
                "API revival disabled: ICMP is unavailable, so the persisted-IP "
                "pre-filter can't run and a verified ONLINE could never demote"
            )
            return False
        return True

    async def _sweep(self) -> None:
        devices = self._monitor._get_devices()
        self._prune()
        candidates = self._select_candidates(devices)
        if not candidates:
            return
        # ``device.ip`` is mutable across every await below (an mDNS
        # Removed clears it, an invalidation callback clears a same-name
        # sibling's); bind each candidate's pair once and act only on it.
        # Deduped by pair: duplicate-name YAMLs normally share one
        # persisted IP, and dialing it once answers for the whole bucket
        # (apply fans out by name) — a second dial would just double the
        # device's slot pressure and double-step the failure backoff.
        seen: set[tuple[str, str]] = set()
        pairs: list[tuple[Device, str]] = []
        for device in candidates:
            key = (device.name, device.ip)
            if key not in seen:
                seen.add(key)
                pairs.append((device, device.ip))
        # Negative pre-filter: batched, cheap, and inadmissible as ONLINE
        # evidence on its own — silence means no dial this sweep.
        rtts = await asyncio.gather(*(self._prefilter(device, ip) for device, ip in pairs))
        now = time.monotonic()
        dials = 0
        for (device, ip), rtt in zip(pairs, rtts, strict=True):
            if rtt is None:
                continue
            if device.ip != ip:
                # The pair we prefiltered is no longer the device's lead
                # (mDNS re-learned it, or an invalidation cleared it) —
                # nothing the echo proved applies to the new address.
                continue
            verified = self._verified.get(device.name)
            if verified is not None and verified[0] == ip and now - verified[1] <= _VERIFIED_TTL:
                # Recently identity-verified at this pair — the echo
                # alone revives, same trust RAM addresses get. A stale
                # pair falls through to a fresh dial instead.
                self._revive(device, ip, rtt)
                continue
            if dials >= _MAX_DIALS_PER_SWEEP:
                # Un-cooled overflow rolls to the next sweep.
                continue
            dials += 1
            await self._verify_and_revive(device, ip, rtt)

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
            and self._cooldown.ready((device.name, device.ip), now)
            and shared.address_resolution_exhausted(self._monitor, device.address)
        ]

    async def _prefilter(self, device: Device, ip: str) -> float | None:
        """ICMP the bound persisted *ip*; cool the pair down on silence.

        The lossy-path retry is worth its packets here: a single
        dropped echo would otherwise park a revivable device for a
        whole ``_ICMP_SILENT_COOLDOWN``.
        """
        # Ping's own semaphore, not a second one: the in-flight ICMP
        # budget is global, so an overlapping sweep and pre-filter
        # can't exceed the icmplib reliability bound together.
        async with self._monitor._ping.icmp_concurrency:
            rtt = await self._monitor._ping.ping_once(ip, retry=True)
        if rtt is None:
            self._cool_down((device.name, ip), _ICMP_SILENT_COOLDOWN)
        return rtt

    async def _verify_and_revive(self, device: Device, ip: str, rtt: float) -> None:
        """One worker dial at the bound *ip*; revive on match, invalidate on mismatch."""
        monitor = self._monitor
        key = (device.name, ip)
        try:
            info = await self._probe(device, [ip])
        except ProbeError as exc:
            # Host-side misses (resolve blip, worker spawn/timeout) say
            # nothing about the device — retry as cheaply as an ICMP
            # miss; a missing Noise key holds until the YAML changes.
            self._cool_down(key, _ICMP_SILENT_COOLDOWN if exc.transient else _NO_KEY_COOLDOWN)
            return
        if info is None:
            # The device itself refused/failed the connect — that's what
            # the escalating backoff is for.
            self._record_dial_failure(key)
            return
        reported = info.get("name", "")
        if not reported:
            # Connected but no identity — inconclusive, not proof of a
            # different device; never burn the only revival lead on it.
            self._record_dial_failure(key)
            return
        if reported != device.name:
            # Whatever holds the lease now is a different device; the
            # persisted IP is proven stale — invalidate it so neither the
            # reviver nor the OTA cache trusts it again. ``reported`` is
            # remote-controlled; truncate it out of the log line.
            _LOGGER.info(
                "Persisted IP %s for %s now answers as %r; invalidating it",
                ip,
                device.name,
                reported[:64],
            )
            # The backoff keeps the stranger un-redialed even when no
            # invalidation callback is wired; the cleared IP then drops
            # the device from the cohort entirely.
            self._record_dial_failure(key)
            if monitor._on_persisted_ip_invalidated is not None:
                monitor._on_persisted_ip_invalidated(device.name, ip)
            return
        mac = _normalize_mac(info.get("mac_address", ""))
        persisted_mac = _normalize_mac(device.mac_address)
        if mac and persisted_mac and mac != persisted_mac:
            _LOGGER.warning(
                "Device at %s reports name %s but MAC %s != persisted %s; not claiming ONLINE",
                ip,
                device.name,
                mac,
                persisted_mac,
            )
            self._record_dial_failure(key)
            return
        self._verified[device.name] = (ip, time.monotonic())
        _LOGGER.info(
            "Revived %s at persisted IP %s (identity verified over the Native API)",
            device.name,
            ip,
        )
        self._revive(device, ip, rtt, info)

    def _revive(
        self, device: Device, ip: str, rtt: float, info: dict[str, Any] | None = None
    ) -> None:
        """Seed the verified *ip*, then claim ONLINE under the ping source.

        IP before state so the first post-revival snapshot carries the
        address and the sweep has its target; the ping nudge hands
        ownership of liveness to the ordinary sweep immediately. A
        fresher mDNS-learned address set that landed mid-dial wins —
        seed only while RAM is still empty.
        """
        monitor = self._monitor
        name = device.name
        if not device.runtime_state.ip_addresses:
            monitor.apply_ip_addresses(name, [ip])
        if info is not None:
            apply_worker_info(monitor, name, info)
        shared.apply_ping_result(monitor, name, rtt)
        monitor.probe_device_ping(name)

    def _cool_down(self, key: tuple[str, str], seconds: float) -> None:
        """Skip this ``(name, ip)`` pair until *seconds* from now."""
        self._cooldown.set(key, seconds)

    def _record_dial_failure(self, key: tuple[str, str]) -> None:
        """Escalating backoff for a responder that answers ICMP but fails the dial."""
        self._cooldown.escalate(key, _DIAL_FAILURE_COOLDOWN, _DIAL_FAILURE_COOLDOWN_MAX)

    def _prune(self) -> None:
        """Drop bookkeeping for gone / recovered / re-IP'd devices.

        An ONLINE transition (any source) or a persisted-IP change
        resets the escalating backoff so legitimate recovery isn't
        delayed by a stale failure streak. Buckets from the name index,
        not a flat map: duplicate ``esphome.name`` YAMLs are distinct
        Devices whose persisted IPs can differ.
        """
        if not (self._cooldown or self._verified):
            return
        get_bucket = self._monitor._get_devices_by_name

        def fresh(key: tuple[str, str]) -> bool:
            return any(
                device.ip == key[1] and device.runtime_state.state is not DeviceState.ONLINE
                for device in get_bucket(key[0])
            )

        self._cooldown.prune(fresh)
        self._verified = {n: v for n, v in self._verified.items() if get_bucket(n)}
