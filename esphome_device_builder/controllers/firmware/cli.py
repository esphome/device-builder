"""Subprocess composition: env, command, cache args, chip verification."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from esphome.core import CORE
from esphome.storage_json import StorageJSON

from ...helpers.remote_build_layout import parse_from_configuration as parse_remote_build_path
from ...helpers.storage_path import resolve_storage_path
from ...models import FirmwareJob, JobType
from . import lifecycle
from .constants import _OTA_ADDRESS_CACHE_JOB_TYPES, ESPHOME_SUBPROCESS_ENV
from .helpers import _find_esptool_cmd

if TYPE_CHECKING:
    from ._state import Lane
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)


def compose_subprocess_env(job: FirmwareJob) -> dict[str, str]:
    """
    Return the env dict for *job*'s ``esphome`` subprocess.

    Layers ``os.environ`` + :data:`ESPHOME_SUBPROCESS_ENV`, then
    for receiver-side remote-build jobs pins ``ESPHOME_DATA_DIR``
    to the per-build subtree under ``CORE.data_dir`` and drops the
    ``ESPHOME_IS_HA_ADDON`` marker so the override is honoured.
    """
    env = {**os.environ, **ESPHOME_SUBPROCESS_ENV}
    remote_build_path = parse_remote_build_path(job.configuration)
    if remote_build_path is not None:
        env["ESPHOME_DATA_DIR"] = str(remote_build_path.data_dir(Path(CORE.data_dir)))
        # esphome's CORE.data_dir prefers is_ha_addon() over ESPHOME_DATA_DIR; an
        # add-on receiver would write artefacts to /data and ignore the override.
        env.pop("ESPHOME_IS_HA_ADDON", None)
    return env


def build_command(
    esphome_cmd: list[str],
    job_type: JobType,
    config_path: str,
    port: str,
    cache_args: list[str] | None = None,
    new_name: str = "",
) -> list[str]:
    """Build the esphome CLI command for a given job type."""
    cmd_map = {
        JobType.COMPILE: "compile",
        JobType.UPLOAD: "upload",
        JobType.INSTALL: "run",
        JobType.CLEAN: "clean",
        JobType.RENAME: "rename",
        # ``clean-all`` takes the config *directory* as its positional,
        # not a YAML file. ``reset_build_env`` queues with
        # ``configuration=""`` so ``rel_path("")`` resolves back to
        # the config_dir at the call site — same shape as the legacy
        # dashboard's ``EsphomeCleanAllHandler``.
        JobType.RESET_BUILD_ENV: "clean-all",
    }
    # cache_args go before the subcommand — esphome's argparse
    # parses them on the top-level parser, not the per-subcommand
    # one. ``--dashboard`` flips ESPHome's log formatter into
    # "escape ANSI as literal text" mode, which survives the
    # colorama strip when stdout is piped to us; the frontend's
    # ansi-log component then un-escapes and renders the colours.
    cmd = [
        *esphome_cmd,
        "--dashboard",
        *(cache_args or []),
        cmd_map[job_type],
        config_path,
    ]
    if job_type == JobType.INSTALL:
        # Without --no-logs the CLI tails logs forever after the
        # upload, never returning — the job would never complete.
        cmd.append("--no-logs")
    if job_type in (JobType.UPLOAD, JobType.INSTALL) and port:
        cmd.extend(["--device", port])
    if job_type == JobType.RENAME:
        # ``esphome rename`` takes the new name as a positional
        # arg. The CLI handles the inner compile + install + old
        # YAML cleanup itself; we let the queue runner stream its
        # output the same way it does for any other build.
        cmd.append(new_name)
    return cmd


def build_cache_args(controller: FirmwareController, job: FirmwareJob) -> list[str]:
    """Return ``--mdns/--dns-address-cache`` args for *job*, or empty."""
    if job.job_type not in _OTA_ADDRESS_CACHE_JOB_TYPES or controller._db.devices is None:
        return []
    # ``rename``'s ``port`` is the post-rename re-install target;
    # the inner ``esphome run`` against the *old* address is
    # always OTA, so skip the gate with ``None``.
    port: str | None = None if job.job_type == JobType.RENAME else job.port
    return controller._db.devices.get_ota_address_cache_args(job.configuration, port)


async def verify_chip(controller: FirmwareController, job: FirmwareJob, lane: Lane) -> None:
    """
    Run ``esptool chip-id`` against *job*'s port and raise on mismatch.

    Skipped for OTA / non-``/dev`` ports and when no
    ``StorageJSON.target_platform`` is recorded yet. Reads
    ``StorageJSON.target_platform`` (upstream-canonical chip
    variant — ``ESP32S3``, ``ESP32C3``, etc.) rather than
    ``Device.target_platform`` (lowercase platform *key* like
    ``esp32``, which would false-positive on a chip-vs-variant
    mismatch).
    """
    if not job.port or job.port.upper() == "OTA" or not job.port.startswith("/dev"):
        return  # only check serial ports

    loop = asyncio.get_running_loop()
    storage = await loop.run_in_executor(
        None, lambda: StorageJSON.load(resolve_storage_path(job.configuration))
    )
    if storage is None or not storage.target_platform:
        return  # never compiled or no platform recorded — nothing to verify

    expected_platform = storage.target_platform.lower()

    async with controller._tracked_subprocess(
        lane,
        *_find_esptool_cmd(),
        "--port",
        job.port,
        "chip-id",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    ) as proc:
        assert proc.stdout is not None  # type narrowing
        output = (await proc.stdout.read()).decode("utf-8", errors="replace")
        await proc.wait()

    # Honour an early cancel that arrived during chip detection
    # (the main install hasn't spawned yet, so the post-wait check
    # in ``_execute_job`` would otherwise let the full install run
    # before reporting CANCELLED). Reusing the ``ValueError`` shape
    # keeps the error path identical to a chip mismatch.
    lifecycle.raise_if_cancelled(controller, job, "chip verification")

    # Parse "Detecting chip type... ESP32-C3"
    detected = ""
    for line in output.splitlines():
        if "Detecting chip type" in line:
            detected = line.split("...")[-1].strip().lower().replace("-", "")
            break

    if not detected:
        _LOGGER.warning("Could not detect chip type on %s", job.port)
        return

    # Normalise: "esp32c3" matches "esp32c3", "esp32" matches "esp32".
    # The target_platform from StorageJSON might be "ESP32S3" (uppercase).
    expected_normalized = expected_platform.lower().replace("-", "").replace("_", "")
    detected_normalized = detected.replace(" ", "")

    if expected_normalized != detected_normalized:
        msg = (
            f"Chip mismatch: config expects {expected_platform} "
            f"but {job.port} has {detected}. Wrong board selected?"
        )
        raise ValueError(msg)

    _LOGGER.debug("Chip verified: %s on %s", detected, job.port)
