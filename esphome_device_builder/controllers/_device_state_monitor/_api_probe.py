"""
Worker-dial plumbing shared by the Native API sources.

The one-shot ``device_info`` probe helpers both ``ApiInfoSource``
and ``ApiReviverSource`` layer over the sweep-loop base.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from typing import TYPE_CHECKING, Any

from ...helpers.device_yaml import DEFAULT_API_PORT
from ...helpers.json import JSONDecodeError, dumps, loads
from ...helpers.subprocess import run_subprocess_capture
from ._sweep_source import SweepSource

if TYPE_CHECKING:
    from ...models import Device
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)

_WORKER_MODULE = "esphome_device_builder.helpers.api_device_info"
_SUBPROCESS_TIMEOUT = 15.0


def api_worker_available() -> bool:
    """
    Report whether the ``aioesphomeapi`` worker can run.

    ``find_spec`` resolves without importing, so ``aioesphomeapi``
    never loads into the dashboard process — only the per-fetch
    worker child imports it.
    """
    return importlib.util.find_spec("aioesphomeapi") is not None


class ApiSweepSource(SweepSource):
    """Sweep-loop base plus the budgeted ``device_info`` worker dial."""

    async def _probe(self, device: Device, addresses: list[str]) -> dict[str, Any] | None:
        """
        Build the request and dial once; payload, or ``None`` on a worker miss.

        ``None`` is a device-side rejection (connect/handshake failed);
        host-side misses raise :class:`ProbeError` instead. The dial
        holds the monitor-wide budget, so the sources' serial loops
        can't overlap into concurrent worker spawns.
        """
        request = await build_probe_request(self._monitor, device, addresses)
        async with self._monitor._api_dial_budget:
            return await self._run_worker(device, request)

    async def _run_worker(self, device: Device, request: bytes) -> dict[str, Any] | None:
        """Instance seam over the shared worker runner (tests stub it here)."""
        return await run_worker(device.name, request)


class ProbeError(Exception):
    """
    The probe couldn't produce a device-side answer.

    ``transient`` marks host-side misses worth a quick retry (key/port
    resolve failure, worker spawn/timeout, garbage worker output);
    ``False`` marks a config declaring Noise encryption with no
    resolvable key, where nothing changes until the YAML does.
    """

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


async def build_probe_request(
    monitor: DeviceStateMonitor, device: Device, addresses: list[str]
) -> bytes:
    """
    Resolve key/port and encode the worker request; raise when undialable.

    An undialable device raises :class:`ProbeError` — a plaintext
    connect without the declared key could only fail the handshake,
    so no doomed worker is spawned.
    """
    noise_psk, port = "", DEFAULT_API_PORT
    if monitor._resolve_api_connection is not None:
        try:
            noise_psk, port = await monitor._resolve_api_connection(device.configuration)
        except Exception as exc:
            _LOGGER.debug("API key/port resolve failed for %s; skipping: %s", device.name, exc)
            raise ProbeError(str(exc), transient=True) from exc
    if device.api_encrypted and not noise_psk:
        _LOGGER.debug("No Native API key resolved for encrypted %s; skipping", device.name)
        raise ProbeError("no Noise key resolved", transient=False)
    return dumps(
        {
            "address": addresses[0],
            "port": port,
            "noise_psk": noise_psk,
            "addresses": addresses,
        }
    )


def apply_worker_info(monitor: DeviceStateMonitor, name: str, info: dict[str, Any]) -> bool:
    """
    Apply a worker payload's mac/version; True iff either was newly written.

    Judged on the ``apply_*`` returns, not a post-apply Device re-read —
    apply dedupes and fans out across same-named devices.
    """
    filled_mac = monitor.apply_mac_address(name, info.get("mac_address", ""))
    filled_version = monitor.apply_version(name, info.get("esphome_version", ""))
    return filled_mac or filled_version


async def run_worker(name: str, request: bytes) -> dict[str, Any] | None:
    """
    Spawn the device-info worker for *name*; payload, ``None``, or raise.

    ``None`` is a device-side rejection: the worker ran cleanly and the
    device refused/failed the connect. Host-side misses (spawn failure,
    subprocess timeout, garbage output) raise a transient
    :class:`ProbeError` — they say nothing about the device and must
    not feed its backoff.
    """
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
        raise ProbeError(f"worker spawn failed: {exc}", transient=True) from exc
    if result.timed_out:
        _LOGGER.debug("API info fetch for %s timed out", name)
        raise ProbeError("worker timed out", transient=True)
    try:
        parsed = loads(result.stdout) if result.stdout else None
    except (JSONDecodeError, ValueError) as exc:
        _LOGGER.debug("API info worker for %s emitted unparsable output: %r", name, result.stdout)
        raise ProbeError("unparsable worker output", transient=True) from exc
    # The worker exits 0 with ``{name, mac_address, esphome_version}`` on
    # success and non-zero with ``{"error": <reason>}`` on a connect/
    # handshake failure — surface that reason so the dominant failure mode
    # is diagnosable instead of silently missing.
    if isinstance(parsed, dict):
        if result.returncode == 0:
            return parsed
        _LOGGER.debug(
            "API info worker for %s failed (rc=%s): %s",
            name,
            result.returncode,
            parsed.get("error") or "no usable output",
        )
        return None
    # No parseable payload at all: the worker broke its contract, which
    # says nothing about the device.
    _LOGGER.debug("API info worker for %s wrote no payload (rc=%s)", name, result.returncode)
    raise ProbeError("worker wrote no payload", transient=True)
