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

# Each spawn imports ``esphome.components`` (~70 MiB RSS). Cap concurrent
# subprocesses so a burst — HA adding several devices, a fleet key-resolve —
# can't stack N×70 MiB on a low-RAM host (HA Green). Callers queue past the cap.
# The process runs a single event loop for its lifetime, so one module-level
# semaphore gates every call site.
_MAX_CONCURRENT_CONFIG = 3
_config_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CONFIG)


class EsphomeConfigUnavailableError(Exception):
    """``esphome config`` could not run to completion.

    A retryable infrastructure fault (spawn failure, timeout, signal kill),
    distinct from a config that ran and failed validation (returns ``None``).
    """


class _SafeLoaderIgnoreUnknown(FastestSafeLoader):
    """SafeLoader that renders unknown ESPHome tags (``!lambda``) as strings."""


def _ignore_unknown(loader: yaml.Loader, node: yaml.Node) -> str:
    # esphome config output emits only scalar custom tags (``!lambda``);
    # a non-scalar node's ``value`` is a node-pair list, so fall back to
    # the bare tag rather than stringifying that into the result.
    if isinstance(node, yaml.ScalarNode):
        return f"{node.tag} {node.value}"
    return node.tag


# ``esphome config`` output still carries ``!lambda`` (and any future
# custom tag) the dumper emits; the catch-all None constructor stringifies
# them so a plain SafeLoader doesn't raise and the result is JSON-native.
_SafeLoaderIgnoreUnknown.add_constructor(None, _ignore_unknown)


async def run_esphome_config(esphome_cmd: list[str], config_path: Path) -> dict[str, Any] | None:
    """
    Run ``esphome config <path> --show-secrets``; return the resolved config.

    Fully resolves substitutions, packages, includes, and secrets — the
    output is plain YAML (no ESPHome ``EFloat`` / ``Lambda`` / ``IncludeFile``
    wrappers). ``--show-secrets`` is required: without it ESPHome conceal-wraps
    secret values in an ANSI SGR.

    Returns the resolved dict on success, ``None`` when the config ran but was
    invalid (non-zero exit) or its output wasn't a mapping, and raises
    :class:`EsphomeConfigUnavailableError` on an infrastructure fault (spawn failure,
    timeout, signal kill) so a caller can answer "retry later" rather than
    "your config is wrong". Failure detail is logged here (never the
    resolved-secret-bearing stderr). Concurrent runs are capped
    (:data:`_MAX_CONCURRENT_CONFIG`); excess callers queue.
    """
    cmd = [*esphome_cmd, "--dashboard", "config", str(config_path), "--show-secrets"]
    # Hold the gate only across the subprocess (where the RAM lives); the
    # parse below runs once it's released.
    async with _config_semaphore:
        try:
            proc = await create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            _LOGGER.warning("esphome config spawn failed for %s: %s", config_path, exc)
            raise EsphomeConfigUnavailableError(f"spawn failed: {exc}") from exc
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_ESPHOME_CONFIG_TIMEOUT)
        except TimeoutError as exc:
            kill_quietly(proc)
            await proc.wait()
            _LOGGER.warning(
                "esphome config timed out after %ss for %s", _ESPHOME_CONFIG_TIMEOUT, config_path
            )
            raise EsphomeConfigUnavailableError("timed out") from exc
        except asyncio.CancelledError:
            # Don't await proc.wait() here: it would suppress / delay the
            # cancellation propagation. The SIGKILL'd child is reaped by
            # asyncio's watcher (matches helpers.subprocess.run_subprocess_capture).
            kill_quietly(proc)
            raise
        returncode = proc.returncode
        if returncode and returncode < 0:
            # Negative == terminated by a signal (crash / OOM kill), not a
            # validation failure — an infrastructure fault, surface it.
            _LOGGER.warning("esphome config killed by signal %s for %s", -returncode, config_path)
            raise EsphomeConfigUnavailableError(f"killed by signal {-returncode}")
        if returncode != 0:
            # A genuinely invalid config the caller surfaces as 422 / empty key —
            # debug, so a user mid-edit doesn't spam the operator's log.
            _LOGGER.debug("esphome config returned %s for %s", returncode, config_path)
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
    if not isinstance(data, dict):
        _LOGGER.debug("esphome config output parsed to %s, not a mapping", type(data).__name__)
        return None
    return data
