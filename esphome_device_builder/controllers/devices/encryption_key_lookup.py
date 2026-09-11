"""Encryption-key and Native API connection resolution for the devices controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...helpers.device_yaml import (
    get_api_port,
    get_resolved_api_encryption_key,
    get_resolved_encryption_key,
    get_resolved_ota_encryption_key,
)
from .resolve import load_config, resolve_config_subprocess

if TYPE_CHECKING:
    from .controller import DevicesController


async def get_encryption_key(controller: DevicesController, configuration: str) -> dict[str, str]:
    """Return ``{"key": ...}`` for *configuration*, api key else esphome OTA key, ``""`` if none."""
    key = get_resolved_encryption_key(await load_config(controller, configuration))
    if not key:
        # A key behind a Jinja-templated package or an ``!include`` resolves only out of process.
        key = get_resolved_encryption_key(
            await resolve_config_subprocess(controller, configuration)
        )
    return {"key": key}


async def get_resolved_api_and_ota_keys(
    controller: DevicesController, configuration: str
) -> tuple[str, str]:
    """Resolve ``(api key, OTA key)`` in process, never via a subprocess; ``""`` if unresolved."""
    config = await load_config(controller, configuration)
    return get_resolved_api_encryption_key(config), get_resolved_ota_encryption_key(config)


async def get_api_connection(controller: DevicesController, configuration: str) -> tuple[str, int]:
    """
    Resolve the Native API ``(encryption_key, port)`` from the on-disk YAML.

    In-process only — unlike :func:`get_encryption_key` this never shells out
    to ``esphome config``, so the background API-info sweep pays no
    per-device subprocess. The key is the api one only: an OTA-side key
    never encrypts the Native API. A device whose key resolves only through
    Jinja-templated ``packages`` returns an empty key here and is left
    for mDNS. Raises :class:`ValueError` when the YAML is missing or
    unparsable so the caller records a miss instead of dialing a doomed
    plaintext/default-port connection it can't have resolved correctly.
    """
    config = await load_config(controller, configuration)
    if config is None:
        raise ValueError(f"could not load YAML for {configuration}")
    return get_resolved_api_encryption_key(config), get_api_port(config)
