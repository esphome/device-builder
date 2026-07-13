"""Plumbing shared by the Native API sources.

The one-shot ``device_info`` probe helpers plus the presence-gated
sweep-loop base both ``ApiInfoSource`` and ``ApiReviverSource`` run on.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import sys
from typing import TYPE_CHECKING, Any

from ...helpers.device_yaml import DEFAULT_API_PORT
from ...helpers.json import JSONDecodeError, dumps, loads
from ...helpers.subprocess import run_subprocess_capture

if TYPE_CHECKING:
    from ...models import Device
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)

_WORKER_MODULE = "esphome_device_builder.helpers.api_device_info"
_SUBPROCESS_TIMEOUT = 15.0
_INTERVAL = 60  # seconds between sweeps


def api_worker_available() -> bool:
    """
    Report whether the ``aioesphomeapi`` worker can run.

    ``find_spec`` resolves without importing, so ``aioesphomeapi``
    never loads into the dashboard process — only the per-fetch
    worker child imports it.
    """
    return importlib.util.find_spec("aioesphomeapi") is not None


class ApiSweepSource:
    """Presence-gated fixed-interval sweep loop; subclasses supply ``_sweep``."""

    # Names the source in the crash-continue log line.
    _sweep_label: str = "API"
    # Head start for the passive sources (mDNS browser, ping sweep) so
    # the common case never reaches this source's heavier repair.
    _bootstrap_delay: float = 0.0

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor
        # Cleared at the top of each iteration so a wake fired
        # mid-sweep still triggers the next idle. The presence 0→1
        # transition is multiplexed into the same event so a
        # subscriber arriving mid-idle doesn't wait out the interval.
        self._wake = asyncio.Event()
        if monitor._presence is not None:
            monitor._presence.add_subscriber_callback(self._wake.set)

    async def run(self) -> None:
        await asyncio.sleep(self._bootstrap_delay)
        if not self._prepare():
            return
        monitor = self._monitor
        # Strict pause when wired to a SubscriberPresence gate: only
        # sweep while at least one dashboard client is subscribed.
        while True:
            if monitor._presence is not None:
                await monitor._presence.wait_for_subscriber()
            self._wake.clear()
            try:
                await self._sweep()
            except Exception:
                # A sweep failure must not kill the loop for the
                # process lifetime; log it and try again next interval.
                _LOGGER.exception("%s sweep failed; continuing", self._sweep_label)
            await self._idle()

    def _prepare(self) -> bool:
        """One-shot gate after the bootstrap sleep; False disables the source."""
        return True

    async def _sweep(self) -> None:
        raise NotImplementedError

    async def _idle(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=_INTERVAL)


class ProbeRequestError(Exception):
    """Key/port resolve failed; transient, the dial may work on a retry."""


async def build_probe_request(
    monitor: DeviceStateMonitor, device: Device, addresses: list[str]
) -> bytes | None:
    """
    Resolve key/port and encode the worker request; ``None`` when undialable.

    ``None`` is definitive until the YAML changes: the config declares
    Noise encryption and no key resolved (e.g. a templated key), so a
    plaintext connect could only fail the handshake. A key/port resolve
    *failure* raises :class:`ProbeRequestError` instead so callers can
    retry it sooner. Either way no doomed worker is spawned.
    """
    noise_psk, port = "", DEFAULT_API_PORT
    if monitor._resolve_api_connection is not None:
        try:
            noise_psk, port = await monitor._resolve_api_connection(device.configuration)
        except Exception as exc:
            _LOGGER.debug("API key/port resolve failed for %s; skipping: %s", device.name, exc)
            raise ProbeRequestError(str(exc)) from exc
    if device.api_encrypted and not noise_psk:
        _LOGGER.debug("No Native API key resolved for encrypted %s; skipping", device.name)
        return None
    return dumps(
        {
            "address": addresses[0],
            "port": port,
            "noise_psk": noise_psk,
            "addresses": addresses,
        }
    )


async def run_worker(name: str, request: bytes) -> dict[str, Any] | None:
    """Spawn the device-info worker for device *name*; parsed payload, or ``None``."""
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
        _LOGGER.debug("Failed to spawn API info worker for %s: %s", name, exc)
        return None
    if result.timed_out:
        _LOGGER.debug("API info fetch for %s timed out", name)
        return None
    try:
        parsed = loads(result.stdout) if result.stdout else None
    except (JSONDecodeError, ValueError):
        _LOGGER.debug("API info worker for %s emitted unparsable output: %r", name, result.stdout)
        return None
    # The worker exits 0 with ``{name, mac_address, esphome_version}`` on
    # success and non-zero with ``{"error": <reason>}`` on a connect/
    # handshake failure — surface that reason so the dominant failure mode
    # is diagnosable instead of silently missing.
    if result.returncode != 0 or not isinstance(parsed, dict):
        reason = parsed.get("error") if isinstance(parsed, dict) else None
        _LOGGER.debug(
            "API info worker for %s failed (rc=%s): %s",
            name,
            result.returncode,
            reason or "no usable output",
        )
        return None
    return parsed
