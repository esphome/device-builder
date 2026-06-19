"""
Native API fallback source for MAC address and ESPHome version.

When mDNS multicast doesn't reach the dashboard (the common Docker-bridge
case) a device can be ONLINE via ping yet have a blank ``mac_address`` /
``deployed_version`` — those fields come only from the ``_esphomelib._tcp``
TXT records. This source connects to such devices over the Native API in a
short-lived subprocess and fills the two fields. It only ever supplies
mac/version; it never drives ONLINE/OFFLINE, so it stays out of the
source-precedence ledger.
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
# Fallback only when no resolver is wired; the real per-device port is
# read from ``api.port`` by the injected ``resolve_api_connection``.
_DEFAULT_API_PORT = 6053
# Consecutive probe failures before one WARNING fires, so a systemically
# broken fallback (resolver bug, worker that never runs, wrong keys fleet-
# wide) surfaces above debug instead of every device silently cooling down.
_SYSTEMIC_FAILURE_WARN_THRESHOLD = 10


class ApiInfoSource:
    """Fill mac/version via the Native API when mDNS hasn't supplied them."""

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor
        self._wake = asyncio.Event()
        # name -> monotonic deadline before which we won't retry a fetch.
        self._cooldown: dict[str, float] = {}
        # Streak of probes that didn't populate; resets on the first full
        # success. The WARNING fires once, when the streak hits the threshold.
        self._consecutive_failures = 0
        if monitor._presence is not None:
            monitor._presence.add_subscriber_callback(self._wake.set)

    async def run(self) -> None:
        # ``find_spec`` resolves without importing, so ``aioesphomeapi``
        # never loads into the dashboard process — only the per-fetch
        # worker child imports it.
        if importlib.util.find_spec("aioesphomeapi") is None:
            _LOGGER.debug("aioesphomeapi not installed; Native API info fallback disabled")
            return
        await asyncio.sleep(_BOOTSTRAP_DELAY)
        monitor = self._monitor
        while True:
            if monitor._presence is not None:
                await monitor._presence.wait_for_subscriber()
            self._wake.clear()
            await self._sweep()
            await self._idle()

    async def _idle(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=_INTERVAL)

    async def _sweep(self) -> None:
        # Strictly one probe at a time: an API connect is far heavier
        # than an ICMP probe, and the fallback is a rare-path repair,
        # not a fleet sweep — serialising keeps it unobtrusive.
        live = {device.name for device in self._monitor._get_devices()}
        self._cooldown = {name: t for name, t in self._cooldown.items() if name in live}
        for device in self._select_targets():
            try:
                await self._fetch(device)
            except Exception:
                # e.g. a select→fetch TOCTOU where an mDNS/ping callback empties
                # the address list between selection and the ``addresses[0]`` read.
                _LOGGER.debug(
                    "API info probe for %s raised; cooling down", device.name, exc_info=True
                )
                self._cooldown[device.name] = time.monotonic() + _FAILURE_COOLDOWN

    def _select_targets(self) -> list[Device]:
        """
        Online, API-capable devices that mDNS isn't supplying mac/version for.

        A device whose ONLINE state is owned by the mDNS source is skipped:
        mDNS is reaching it, so mac/version arrive on the ``_esphomelib._tcp``
        TXT records for free. We probe only when mDNS isn't delivering, the
        device still misses a field, it's off cooldown, and it has a routable
        IP — never once both fields are already known.
        """
        now = time.monotonic()
        monitor = self._monitor
        return [
            device
            for device in monitor._get_devices()
            if device.state is DeviceState.ONLINE
            and "api" in device.loaded_integrations
            and monitor.priority_for(device.name) != ReachabilitySource.MDNS
            and not (device.mac_address and device.deployed_version)
            and self._cooldown.get(device.name, 0.0) <= now
            and self._candidate_addresses(device)
        ]

    @staticmethod
    def _candidate_addresses(device: Device) -> list[str]:
        """
        Dial addresses for *device*, IPv4 primary first; empty for a bare ``.local`` name.

        Leads with ``device.ip`` (the IPv4 primary the monitor already
        picked via ``_pick_ipv4``) so the worker doesn't dial a
        link-local IPv6 first, then appends the rest of the announced set.
        """
        if device.ip or device.ip_addresses:
            primary = [device.ip] if device.ip else []
            return primary + [addr for addr in device.ip_addresses if addr != device.ip]
        if device.address and not is_local_hostname(device.address):
            return [device.address]
        return []

    async def _fetch(self, device: Device) -> None:
        monitor = self._monitor
        addresses = self._candidate_addresses(device)
        noise_psk, port = "", _DEFAULT_API_PORT
        if monitor._resolve_api_connection is not None:
            try:
                noise_psk, port = await monitor._resolve_api_connection(device.configuration)
            except Exception as exc:  # noqa: BLE001 — best-effort; fall back to plaintext/default
                _LOGGER.debug(
                    "API key/port resolve failed for %s; using plaintext/%d: %s",
                    device.name,
                    _DEFAULT_API_PORT,
                    exc,
                )
        request = json.dumps(
            {
                "address": addresses[0],
                "port": port,
                "noise_psk": noise_psk,
                "addresses": addresses,
            }
        )
        info = await self._run_worker(device, request) or {}
        mac = info.get("mac_address", "")
        version = info.get("esphome_version", "")
        monitor.apply_mac_address(device.name, mac)
        monitor.apply_version(device.name, version)
        # Judge success on what the worker actually delivered, not a post-apply
        # re-read of the Device — ``apply_*`` dedupes and fans out across
        # same-named devices, so a re-read could miscount a real hit as a miss.
        # A full hit clears the streak; anything short backs the device off and
        # feeds the systemic-failure counter (one WARNING when it's dead fleet-
        # wide, which would otherwise be debug-only).
        if mac and version:
            self._consecutive_failures = 0
            return
        self._cooldown[device.name] = time.monotonic() + _FAILURE_COOLDOWN
        self._consecutive_failures += 1
        # Strictly +1 per failure and reset to 0 on success, so equality is
        # hit exactly once per streak — one WARNING, no per-device spam.
        if self._consecutive_failures == _SYSTEMIC_FAILURE_WARN_THRESHOLD:
            _LOGGER.warning(
                "Native API info fallback has failed for %d devices in a row; "
                "MAC/version may stay blank — check device API reachability, "
                "encryption keys, and the api.port setting",
                self._consecutive_failures,
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
