"""Resolve a device config through the ``esphome config`` subprocess."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from ..subprocess import create_subprocess_exec, kill_quietly
from ..yaml import FastestSafeLoader

_LOGGER = logging.getLogger(__name__)

# ``esphome config`` validates the full config (imports components,
# resolves packages); the first run on a cold process can take a few
# seconds. 60s is generous headroom over that without hanging a request
# forever on a wedged subprocess.
_ESPHOME_CONFIG_TIMEOUT = 60.0


class _SafeLoaderIgnoreUnknown(FastestSafeLoader):
    """SafeLoader that renders unknown ESPHome tags (``!lambda``) as strings."""


def _ignore_unknown(loader: yaml.Loader, node: yaml.Node) -> str:
    return f"{node.tag} {node.value}"


# ``esphome config`` output still carries ``!lambda`` (and any future
# custom tag) the dumper emits; the catch-all None constructor stringifies
# them so a plain SafeLoader doesn't raise and the result is JSON-native.
_SafeLoaderIgnoreUnknown.add_constructor(None, _ignore_unknown)


async def run_esphome_config(esphome_cmd: list[str], config_path: Path) -> dict[str, Any] | None:
    """
    Run ``esphome config <path> --show-secrets``; return the resolved config.

    Fully resolves substitutions, packages, includes, and secrets — the
    output is plain YAML (no ESPHome ``EFloat`` / ``Lambda`` / ``IncludeFile``
    wrappers). Returns ``None`` when the spawn failed, the command exited
    non-zero, or the output didn't parse to a mapping. ``--show-secrets`` is
    required: without it ESPHome conceal-wraps secret values in an ANSI SGR.

    Failure detail is logged here (never the resolved-secret-bearing stderr)
    rather than returned, so callers only branch on ``None``.
    """
    cmd = [*esphome_cmd, "--dashboard", "config", str(config_path), "--show-secrets"]
    try:
        proc = await create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        _LOGGER.debug("esphome config spawn failed for %s: %s", config_path, exc)
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_ESPHOME_CONFIG_TIMEOUT)
    except TimeoutError:
        kill_quietly(proc)
        await proc.wait()
        _LOGGER.debug("esphome config timed out for %s", config_path)
        return None
    except asyncio.CancelledError:
        kill_quietly(proc)
        raise
    if proc.returncode != 0:
        _LOGGER.debug("esphome config returned %s for %s", proc.returncode, config_path)
        return None
    return _parse_resolved_config(stdout)


def _parse_resolved_config(stdout: bytes) -> dict[str, Any] | None:
    """Parse ``esphome config`` output; ``None`` on YAML error or non-mapping."""
    try:
        data = yaml.load(stdout, Loader=_SafeLoaderIgnoreUnknown)  # noqa: S506
    except yaml.YAMLError as exc:
        # Log the exception class only; ``str(yaml.YAMLError)`` echoes context
        # lines from ``--show-secrets`` output, which carry resolved secrets.
        _LOGGER.debug("esphome config output did not parse as YAML (%s)", type(exc).__name__)
        return None
    return data if isinstance(data, dict) else None
