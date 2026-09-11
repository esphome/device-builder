"""Encryption-key and Native API connection resolution for the devices controller."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NamedTuple

from ...helpers.device_yaml import (
    ESPHOME_CONFIG_TIMEOUT,
    get_api_port,
    get_resolved_api_encryption_key,
    get_resolved_encryption_key,
    get_resolved_ota_encryption_key,
    ota_encryption_block_unresolved,
)
from .resolve import load_config, resolve_config_subprocess

_LOGGER = logging.getLogger(__name__)


class ResolvedKeys(NamedTuple):
    """In-process view of a config's keys; ``ota_unreadable`` flags an OTA block left a string."""

    api: str
    ota: str
    ota_unreadable: bool


if TYPE_CHECKING:
    from pathlib import Path

    from .controller import DevicesController


async def get_encryption_key(controller: DevicesController, configuration: str) -> dict[str, str]:
    """Return ``{"key": ...}`` for *configuration*, api key else esphome OTA key, ``""`` if none."""
    # A key behind a Jinja-templated package or an ``!include`` resolves only out of process;
    # an infra fault and a keyless config both collapse to the ``""`` the UI reads as
    # "open the editor and check".
    path, config = await load_config(controller, configuration)
    key = get_resolved_encryption_key(config) or get_resolved_encryption_key(
        await resolve_config_subprocess(controller, path)
    )
    return {"key": key}


async def get_resolved_api_and_ota_keys(
    controller: DevicesController,
    configuration: str | Path,
    *,
    timeout: float | None = None,
) -> ResolvedKeys:
    """Resolve the api and OTA keys in process, never via a subprocess; ``""`` if unresolved."""
    if timeout is None:
        timeout = ESPHOME_CONFIG_TIMEOUT
    try:
        # Bounded like the subprocess: a never-cloned package makes the loader
        # fetch it, and git carries no timeout of its own. A timeout frees the
        # caller, not the executor thread.
        _, config = await asyncio.wait_for(load_config(controller, configuration), timeout=timeout)
    except TimeoutError:
        _LOGGER.warning(
            "In-process resolve of %s exceeded %ss; keys treated as unresolved",
            configuration,
            timeout,
        )
        return ResolvedKeys(api="", ota="", ota_unreadable=False)
    return ResolvedKeys(
        get_resolved_api_encryption_key(config),
        get_resolved_ota_encryption_key(config),
        ota_encryption_block_unresolved(config),
    )


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
    _, config = await load_config(controller, configuration)
    if config is None:
        raise ValueError(f"could not load YAML for {configuration}")
    return get_resolved_api_encryption_key(config), get_api_port(config)
