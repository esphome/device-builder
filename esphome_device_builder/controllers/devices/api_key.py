"""Native API encryption-key resolution for the devices controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import (
    EsphomeConfigUnavailableError,
    get_api_port,
    get_resolved_api_encryption_key,
    load_device_yaml,
    run_esphome_config,
)

if TYPE_CHECKING:
    from .controller import DevicesController


async def get_api_key(controller: DevicesController, configuration: str) -> dict[str, str]:
    """
    Return the resolved Native API encryption key for *configuration*.

    Tries the in-process YAML loader first, then falls back to
    ``esphome config --show-secrets`` for configs whose key is
    constructed by Jinja-templated ``packages`` (issue #437).
    Returns ``{"key": ""}`` when both paths fail; the caller
    treats that as the "open the editor and check" signal.
    """
    path = controller._db.settings.rel_path(configuration)
    config = await run_in_executor(load_device_yaml, path)
    key = get_resolved_api_encryption_key(config)
    if key:
        return {"key": key}
    key = await resolve_via_esphome_config(controller, configuration)
    return {"key": key}


async def get_api_connection(controller: DevicesController, configuration: str) -> tuple[str, int]:
    """
    Resolve the Native API ``(encryption_key, port)`` from the on-disk YAML.

    In-process only — unlike :func:`get_api_key` this never shells out
    to ``esphome config``, so the background API-info sweep pays no
    per-device subprocess. A device whose key resolves only through
    Jinja-templated ``packages`` returns an empty key here and is left
    for mDNS. Raises :class:`ValueError` when the YAML is missing or
    unparsable so the caller records a miss instead of dialing a doomed
    plaintext/default-port connection it can't have resolved correctly.
    """
    path = controller._db.settings.rel_path(configuration)
    config = await run_in_executor(load_device_yaml, path)
    if config is None:
        raise ValueError(f"could not load YAML for {configuration}")
    return get_resolved_api_encryption_key(config), get_api_port(config)


async def resolve_via_esphome_config(controller: DevicesController, configuration: str) -> str:
    """
    Subprocess fallback for :func:`get_api_key`.

    Delegates to :func:`helpers.device_yaml.run_esphome_config`, which fully
    resolves substitutions / packages / secrets. Returns ``""`` on every
    failure path — an infra fault and a keyless config both collapse to the
    "open the editor and check" sentinel the UI already handles.
    """
    esphome_cmd = controller.state.esphome_cmd
    if not esphome_cmd:
        return ""
    config_path = controller._db.settings.rel_path(configuration)
    try:
        config = await run_esphome_config(esphome_cmd, config_path)
    except EsphomeConfigUnavailableError:
        return ""
    if config is None:
        return ""
    return get_resolved_api_encryption_key(config)
