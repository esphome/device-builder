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
    r"""
    Return the resolved Native API encryption key for *configuration*.

    Two-stage resolution:

    1. Fast path — ``yaml_util.load_yaml`` + package merge in-process.
       Resolves ``!secret`` / ``!include`` / packages the same way
       the rest of the dashboard does. Covers the common case
       (key directly in YAML or behind a ``!secret`` reference)
       with no subprocess overhead.

    2. Slow path — ``esphome --dashboard config <file> --show-secrets``
       subprocess. Falls back here when the fast path returns ``""``,
       which happens for configs whose key is constructed by an
       ESPHome preprocessor feature the dashboard's loader doesn't
       reproduce. The canonical example is Jinja-templated
       packages (``api: |\\n  # set ns = ... ${ns.cfg}``) — issue
       #437. ESPHome's full pipeline runs the Jinja step before
       YAML parsing, so its ``config`` subcommand emits a
       fully-resolved YAML on stdout that we parse to pull the
       key out. Slow (~1s subprocess) but only on click, not per
       scan, and only when the fast path fails.

    ``{"key": "<base64 32-byte>"}`` on success; ``{"key": ""}`` when
    both paths fail (no ``api:`` block, no ``encryption`` key, YAML
    loading fails, or the subprocess errors out). Callers treat
    the empty value as the "open the editor and check" signal.
    """
    path = controller._db.settings.rel_path(configuration)
    loop = asyncio.get_running_loop()
    config = await loop.run_in_executor(None, load_device_yaml, path)
    key = get_api_encryption_key(config)
    if key:
        return {"key": key}
    # Fast path missed — subprocess to ESPHome's full
    # ``config`` pipeline (which runs the Jinja preprocessor
    # over packages) and parse its resolved-YAML output.
    key = await resolve_via_esphome_config(controller, configuration)
    return {"key": key}


async def resolve_via_esphome_config(controller: DevicesController, configuration: str) -> str:
    r"""
    Subprocess fallback for ``get_api_key``.

    Runs ``esphome --dashboard config <path> --show-secrets``,
    captures stdout, parses it as YAML, and returns
    ``api.encryption.key`` if present. ``--show-secrets`` is
    required: without it, ``esphome config`` wraps each
    ``key`` value in the ANSI conceal SGR (``\\x1b[8m...\\x1b[28m``)
    and ``yaml.safe_load`` would treat the wrapped string as the
    key value. The wire form ESPHome emits when secrets are
    shown is the literal base64 we want.

    Returns ``""`` on any failure path: subprocess startup
    failure (``controller._esphome_cmd`` empty / unreachable),
    non-zero exit (config didn't validate), stdout that doesn't
    parse as YAML, missing api / encryption block. The caller
    rolls all of these into the documented "open the editor and
    check" signal — there's no actionable distinction between
    "config invalid" and "no encryption" at the API surface.
    """
    # Defensive ``getattr``: bypass-init controllers used by
    # tests that don't go through ``start()`` (the
    # ``make_controller`` factory in
    # ``tests/controllers/devices/conftest.py``) don't set
    # ``_esphome_cmd`` unless explicitly told to. Production
    # always sets it in ``start()`` so the attribute is
    # guaranteed there.
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
        # ``str(yaml.YAMLError)`` includes context lines from
        # the input, which were emitted with ``--show-secrets``
        # and therefore carry the resolved Wi-Fi password,
        # API key, etc. verbatim. Log only the exception class
        # name so a malformed-output failure surfaces in debug
        # logs without leaking those secrets into the operator's
        # log scrape / support bundle.
        _LOGGER.debug(
            "esphome config output for %s did not parse as YAML (%s)",
            configuration,
            type(exc).__name__,
        )
        return ""
    return get_api_encryption_key(resolved)
