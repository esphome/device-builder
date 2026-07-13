"""One-shot Native API ``device_info`` probe plumbing shared by the API sources."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

from ...helpers import json
from ...helpers.device_yaml import DEFAULT_API_PORT
from ...helpers.subprocess import run_subprocess_capture

if TYPE_CHECKING:
    from ...models import Device
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)

_WORKER_MODULE = "esphome_device_builder.helpers.api_device_info"
_SUBPROCESS_TIMEOUT = 15.0


async def build_probe_request(
    monitor: DeviceStateMonitor, device: Device, addresses: list[str]
) -> bytes | None:
    """
    Resolve key/port and encode the worker request; ``None`` when undialable.

    ``None`` means the dial is doomed before it starts — the key/port
    resolve failed, or the config declares Noise encryption and no key
    resolved (e.g. a templated key), so a plaintext connect could only
    fail the handshake. Callers record the miss instead of spawning a
    doomed worker.
    """
    noise_psk, port = "", DEFAULT_API_PORT
    if monitor._resolve_api_connection is not None:
        try:
            noise_psk, port = await monitor._resolve_api_connection(device.configuration)
        except Exception as exc:  # noqa: BLE001 — can't resolve how to reach the device
            _LOGGER.debug("API key/port resolve failed for %s; skipping: %s", device.name, exc)
            return None
    if device.api_encrypted and not noise_psk:
        _LOGGER.debug("No Native API key resolved for encrypted %s; skipping", device.name)
        return None
    return json.dumps(
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
        parsed = json.loads(result.stdout) if result.stdout else None
    except (json.JSONDecodeError, ValueError):
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
