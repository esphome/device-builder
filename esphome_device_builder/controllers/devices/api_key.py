"""Native API encryption-key resolution for the devices controller."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import yaml

from ...helpers.device_yaml import get_api_encryption_key, load_device_yaml
from ...helpers.subprocess import create_subprocess_exec

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


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
    loop = asyncio.get_running_loop()
    config = await loop.run_in_executor(None, load_device_yaml, path)
    key = get_api_encryption_key(config)
    if key:
        return {"key": key}
    key = await resolve_via_esphome_config(controller, configuration)
    return {"key": key}


async def resolve_via_esphome_config(controller: DevicesController, configuration: str) -> str:
    r"""
    Subprocess fallback for :func:`get_api_key`.

    ``--show-secrets`` is required: without it ESPHome wraps each
    secret value in the ANSI conceal SGR (``\x1b[8m...\x1b[28m``)
    and ``yaml.safe_load`` would treat the wrapped string as the
    key. Returns ``""`` on every failure path.
    """
    # Defensive ``getattr``: bypass-init test controllers skip
    # ``__init__`` (which is what creates the attribute).
    esphome_cmd: list[str] | None = getattr(controller, "_esphome_cmd", None)
    if not esphome_cmd:
        return ""
    config_path = str(controller._db.settings.rel_path(configuration))
    cmd = [*esphome_cmd, "--dashboard", "config", config_path, "--show-secrets"]
    try:
        proc = await create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout_bytes, _ = await proc.communicate()
    except OSError as exc:
        _LOGGER.debug("esphome config subprocess failed for %s: %s", configuration, exc)
        return ""
    if proc.returncode != 0:
        _LOGGER.debug(
            "esphome config returned %s for %s; key extraction skipped",
            proc.returncode,
            configuration,
        )
        return ""
    try:
        resolved = yaml.safe_load(stdout_bytes.decode("utf-8", errors="replace"))
    except yaml.YAMLError as exc:
        # Log the exception class only; ``str(yaml.YAMLError)``
        # includes context lines from the ``--show-secrets``
        # output, which carry resolved Wi-Fi passwords / API
        # keys verbatim and would leak into log scrapes.
        _LOGGER.debug(
            "esphome config output for %s did not parse as YAML (%s)",
            configuration,
            type(exc).__name__,
        )
        return ""
    return get_api_encryption_key(resolved)
