"""
Native API fallback source for MAC address and ESPHome version.

When mDNS multicast doesn't reach the dashboard (the common Docker-bridge
case) a device can be ONLINE via ping yet have a blank ``mac_address`` /
``deployed_version`` — those fields come only from the ``_esphomelib._tcp``
TXT records. Each sweep first re-applies zeroconf-cached TXT payloads for
free (the browser handler can miss an announce whose records still landed in
the cache), then connects to still-blank devices over the Native API in a
short-lived subprocess. It only ever supplies the TXT-derived fields; it
never drives ONLINE/OFFLINE, so it stays out of the source-precedence
ledger.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import sys
import time
from typing import TYPE_CHECKING, Any

from ...helpers import json
from ...helpers.device_yaml import DEFAULT_API_PORT
from ...helpers.hostname import is_local_hostname
from ...helpers.subprocess import run_subprocess_capture
from ...models import Device, DeviceState, ReachabilitySource

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)

_WORKER_MODULE = "esphome_device_builder.helpers.api_device_info"
_INTERVAL = 60  # seconds between fallback sweeps
# Give mDNS a head start so devices that announce normally fill
# mac/version for free and never trigger a connection.
_BOOTSTRAP_DELAY = 15
# Per-device backoff after a failed fetch so an unreachable / wrong-key
# / non-API device isn't reconnected every sweep.
_FAILURE_COOLDOWN = 600  # seconds
_SUBPROCESS_TIMEOUT = 15.0
# Max devices probed per sweep. Each probe is serial and can run the full
# subprocess timeout, so an mDNS-dark all-failing fleet would otherwise spawn
# interpreters back-to-back for minutes; the overflow rolls to the next sweep.
_MAX_PROBES_PER_SWEEP = 8
# Distinct devices stuck failing (due but on cooldown) before one WARNING
# fires, so a systemically broken fallback (resolver bug, worker that never
# runs, wrong keys for a large subset) surfaces above debug — and a single
# healthy device elsewhere can't mask it.
_SYSTEMIC_FAILURE_WARN_THRESHOLD = 10


class ApiInfoSource:
    """Fill mac/version via the Native API when mDNS hasn't supplied them."""

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor
        self._wake = asyncio.Event()
        # name -> monotonic deadline before which we won't retry a fetch.
        self._cooldown: dict[str, float] = {}
        # Device names to probe once even though they already have mac+version
        # (post-flash version verification); cleared after one probe attempt.
        self._force_reprobe: set[str] = set()
        # One-shot latch for the systemic WARNING; re-arms once the count of
        # distinct devices stuck failing drops back below the threshold.
        self._warned_systemic = False
        # Re-checked by ``run``; without aioesphomeapi the sweep still runs
        # its mDNS-cache reconcile but skips the API-connect stage.
        self._api_available = True
        if monitor._presence is not None:
            monitor._presence.add_subscriber_callback(self._wake.set)

    def request_reprobe(self, name: str) -> None:
        """Force one probe of *name* on the next sweep, ignoring the mac+version guard."""
        self._force_reprobe.add(name)
        self._wake.set()

    async def run(self) -> None:
        # ``find_spec`` resolves without importing, so ``aioesphomeapi``
        # never loads into the dashboard process — only the per-fetch
        # worker child imports it. The sweep loop still runs without it:
        # the mDNS-cache reconcile pass needs no API worker.
        self._api_available = importlib.util.find_spec("aioesphomeapi") is not None
        if not self._api_available:
            _LOGGER.debug(
                "aioesphomeapi not installed; Native API connect stage disabled "
                "(mDNS-cache reconcile still active)"
            )
        await asyncio.sleep(_BOOTSTRAP_DELAY)
        monitor = self._monitor
        while True:
            if monitor._presence is not None:
                await monitor._presence.wait_for_subscriber()
            self._wake.clear()
            try:
                await self._sweep()
            except Exception:
                # A failure outside the per-device guard (``_select_targets``,
                # the cooldown prune, the health check) must not kill the loop
                # for the process lifetime; log it and try again next interval.
                _LOGGER.exception("API info sweep failed; continuing")
            await self._idle()

    async def _idle(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=_INTERVAL)

    async def _sweep(self) -> None:
        # Strictly one probe at a time: an API connect is far heavier
        # than an ICMP probe, and the fallback is a rare-path repair,
        # not a fleet sweep — serialising keeps it unobtrusive.
        devices = self._monitor._get_devices()
        live = {device.name for device in devices}
        self._cooldown = {name: t for name, t in self._cooldown.items() if name in live}
        self._force_reprobe &= live
        # Free repair first: a device the browser handler missed (timed-out
        # resolve, cold-start probe no-op) sits blank while the zeroconf cache
        # holds its TXT, and same-content TTL refreshes never re-fire the
        # handler. Devices the cache fills drop out of ``_select_targets``.
        self._reconcile_from_mdns_cache(devices)
        if not self._api_available:
            return
        # Cap probes per sweep so an mDNS-dark fleet where every probe runs
        # the full subprocess timeout doesn't churn out back-to-back
        # interpreter spawns for minutes. The overflow rolls to the next
        # interval; failures cool down and drop out, so the fleet drains in
        # bounded bursts.
        targets = self._select_targets()
        if len(targets) > _MAX_PROBES_PER_SWEEP:
            _LOGGER.debug(
                "API info: probing %d of %d due devices this sweep; %d roll to the next",
                _MAX_PROBES_PER_SWEEP,
                len(targets),
                len(targets) - _MAX_PROBES_PER_SWEEP,
            )
        for device in targets[:_MAX_PROBES_PER_SWEEP]:
            try:
                await self._fetch(device)
            except Exception:
                # The benign select→fetch race (emptied address list) is handled
                # inside ``_fetch``; anything reaching here is unexpected (a real
                # bug), so log at WARNING rather than masking it as a debug miss.
                _LOGGER.warning(
                    "API info probe for %s raised unexpectedly; cooling down",
                    device.name,
                    exc_info=True,
                )
                self._record_failure(device)
        self._evaluate_systemic_health()

    def _reconcile_from_mdns_cache(self, devices: list[Device]) -> None:
        """Re-apply cached TXT payloads for online devices missing monitor fields."""
        monitor = self._monitor
        # ``deployed_config_hash`` and the ``api_encryption_active``
        # tri-state (``None`` = never observed) widen this gate beyond
        # ``_is_due``'s mac+version: the cached TXT carries them but the
        # API worker can't fetch them. A non-API device is served by the
        # ``_http._tcp`` identity TXT instead, which carries no
        # api_encryption, so only the identity fields gate it.
        names = {
            device.name
            for device in devices
            if device.runtime_state.state is DeviceState.ONLINE
            and (
                (device.api_enabled and device.runtime_state.api_encryption_active is None)
                or not (
                    device.mac_address
                    and device.runtime_state.deployed_version
                    and device.runtime_state.deployed_config_hash
                )
            )
        }
        for name in sorted(names):
            monitor.reconcile_from_mdns_cache(name)

    def _is_due(self, device: Device) -> bool:
        """
        Report whether *device* needs an API probe, ignoring cooldown.

        Due means: online, exposes a Native API, reachable by IP, and either
        still missing a field or flagged for a forced re-probe (post-flash
        version verification, which probes even when both fields are filled).
        Only the forced case defers to mDNS ownership — the announce carries
        the fields, so the re-probe is redundant there. The missing-field case
        deliberately doesn't: ownership proves an announce resolved once, not
        that its TXT payload was ever applied (#1910), and the sweep's cache
        reconcile has already run, so reaching here means the cache can't fill
        the gap.
        """
        monitor = self._monitor
        runtime = device.runtime_state
        return (
            runtime.state is DeviceState.ONLINE
            and device.api_enabled
            and (
                not (device.mac_address and runtime.deployed_version)
                or (
                    device.name in self._force_reprobe
                    and monitor.priority_for(device.name) != ReachabilitySource.MDNS
                )
            )
            and bool(self._candidate_addresses(device))
        )

    def _select_targets(self) -> list[Device]:
        """
        Due devices that are off cooldown — the probe candidates for this sweep.

        A forced re-probe ignores cooldown: it's a deliberate one-shot request.
        """
        now = time.monotonic()
        return [
            device
            for device in self._monitor._get_devices()
            if self._is_due(device)
            and (device.name in self._force_reprobe or self._cooldown.get(device.name, 0.0) <= now)
        ]

    @staticmethod
    def _candidate_addresses(device: Device) -> list[str]:
        """
        Dial addresses for *device*, IPv4 primary first; empty for a bare ``.local`` name.

        Leads with ``device.ip`` (the IPv4 primary the monitor already
        picked via ``_pick_ipv4``) so the worker doesn't dial a
        link-local IPv6 first, then appends the rest of the announced set.
        """
        addresses = device.runtime_state.ip_addresses
        if device.ip or addresses:
            primary = [device.ip] if device.ip else []
            return primary + [addr for addr in addresses if addr != device.ip]
        if device.address and not is_local_hostname(device.address):
            return [device.address]
        return []

    async def _fetch(self, device: Device) -> None:
        monitor = self._monitor
        # One-shot, consumed up front — before the dial / key-resolve
        # early-returns below — so a device we can't even reach (no address,
        # unresolvable Noise key) isn't force-probed every sweep, since a
        # forced probe bypasses cooldown. The trade-off is deliberate: that
        # device's post-flash rollback check is skipped, but it can't be
        # API-verified anyway.
        forced = device.name in self._force_reprobe
        self._force_reprobe.discard(device.name)
        addresses = self._candidate_addresses(device)
        if not addresses:
            # select→fetch TOCTOU: an mDNS/ping callback emptied the address
            # list after selection. Back off rather than indexing an empty list.
            self._record_failure(device)
            return
        noise_psk, port = "", DEFAULT_API_PORT
        if monitor._resolve_api_connection is not None:
            try:
                noise_psk, port = await monitor._resolve_api_connection(device.configuration)
            except Exception as exc:  # noqa: BLE001 — can't resolve how to reach the device
                # A plaintext/default guess would only fail the handshake;
                # record the miss instead of spawning a doomed worker.
                _LOGGER.debug("API key/port resolve failed for %s; skipping: %s", device.name, exc)
                self._record_failure(device)
                return
        if device.api_encrypted and not noise_psk:
            # The config declares Noise encryption but no key resolved (e.g. a
            # templated key) — a plaintext connect can only fail the handshake.
            _LOGGER.debug("No Native API key resolved for encrypted %s; skipping", device.name)
            self._record_failure(device)
            return
        request = json.dumps(
            {
                "address": addresses[0],
                "port": port,
                "noise_psk": noise_psk,
                "addresses": addresses,
            }
        )
        info = await self._run_worker(device, request) or {}
        # ``apply_*`` returns True iff it newly wrote the field. Judge on that,
        # not a post-apply Device re-read (apply dedupes / fans out across
        # same-named devices). Any newly-filled field means the connection
        # worked and made progress: don't cool down, so a device that answered
        # with mac XOR version chases the rest on the next normal sweep. Nothing
        # newly filled (connect failed, or only a value we already had) is a
        # real miss → cool the device down.
        filled_mac = monitor.apply_mac_address(device.name, info.get("mac_address", ""))
        filled_version = monitor.apply_version(device.name, info.get("esphome_version", ""))
        if filled_mac or filled_version:
            return
        # A forced re-probe that connected (``info`` truthy) but changed
        # nothing confirmed the existing version — a success, not a miss, so
        # don't cool it down. The normal path still cools down here: it was
        # due *because* a field was missing, so "nothing newly filled" is a
        # real miss to retry later.
        if forced and info:
            return
        self._record_failure(device)

    def _record_failure(self, device: Device) -> None:
        """Back *device* off so the next sweep skips it until the cooldown expires."""
        self._cooldown[device.name] = time.monotonic() + _FAILURE_COOLDOWN

    def _evaluate_systemic_health(self) -> None:
        """
        Warn once when too many *distinct* devices are stuck failing; re-arm on recovery.

        Counts devices that are due *and* currently on cooldown — i.e. genuinely
        failing right now — by cross-referencing live eligibility, so a device
        that recovered (mDNS filled it, went offline, or was deleted) drops out
        and a single healthy probe elsewhere can't mask a persistently broken
        subset (which a fleet-wide success streak could).
        """
        now = time.monotonic()
        failing = sum(
            1
            for device in self._monitor._get_devices()
            if self._is_due(device) and self._cooldown.get(device.name, 0.0) > now
        )
        if failing < _SYSTEMIC_FAILURE_WARN_THRESHOLD:
            self._warned_systemic = False
            return
        if not self._warned_systemic:
            self._warned_systemic = True
            _LOGGER.warning(
                "Native API info fallback is failing for %d devices; MAC/version "
                "may stay blank — check device API reachability, encryption keys, "
                "and the api.port setting",
                failing,
            )

    async def _run_worker(self, device: Device, request: bytes) -> dict[str, Any] | None:
        try:
            result = await run_subprocess_capture(
                sys.executable,
                "-m",
                _WORKER_MODULE,
                timeout=_SUBPROCESS_TIMEOUT,
                stdin_data=request,
                merge_stderr=False,
            )
        except OSError as exc:
            _LOGGER.debug("Failed to spawn API info worker for %s: %s", device.name, exc)
            return None
        if result.timed_out:
            _LOGGER.debug("API info fetch for %s timed out", device.name)
            return None
        try:
            parsed = json.loads(result.stdout) if result.stdout else None
        except (json.JSONDecodeError, ValueError):
            _LOGGER.debug(
                "API info worker for %s emitted unparsable output: %r", device.name, result.stdout
            )
            return None
        # The worker exits 0 with ``{mac_address, version}`` on success and
        # non-zero with ``{"error": <reason>}`` on a connect/handshake
        # failure — surface that reason so the dominant failure mode is
        # diagnosable instead of silently missing.
        if result.returncode != 0 or not isinstance(parsed, dict):
            reason = parsed.get("error") if isinstance(parsed, dict) else None
            _LOGGER.debug(
                "API info worker for %s failed (rc=%s): %s",
                device.name,
                result.returncode,
                reason or "no usable output",
            )
            return None
        return parsed
