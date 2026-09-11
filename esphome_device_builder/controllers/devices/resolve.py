"""Resolve a configuration's YAML in process, spawning ``esphome config`` only as a fallback."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import (
    ESPHOME_CONFIG_TIMEOUT,
    EsphomeConfigUnavailableError,
    load_device_yaml,
    resolution_incomplete,
    run_esphome_config,
)

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..config.settings import DashboardSettings
    from .controller import DevicesController


async def resolve_config(controller: DevicesController, path: Path) -> dict[Any, Any] | None:
    """Load in process, then ``esphome config`` if work was deferred; ``None`` if neither works."""
    _, config = await load_config(controller, path)
    if resolution_incomplete(config):
        config = await resolve_config_subprocess(controller, path)
    return config


async def load_config(
    controller: DevicesController, configuration: str | Path, *, timeout: float | None = None
) -> tuple[Path, dict[Any, Any] | None]:
    """Locate and load in one hop under a ceiling; config ``None`` if unparsable or stalled."""
    if timeout is None:
        timeout = ESPHOME_CONFIG_TIMEOUT
    try:
        # Same ceiling as the subprocess: a never-cloned package makes the loader
        # fetch it, and git carries no timeout of its own. A timeout frees the
        # caller, not the executor thread, and counts as deferred work.
        return await asyncio.wait_for(
            run_in_executor(_locate_and_load, controller._db.settings, configuration),
            timeout=timeout,
        )
    except TimeoutError:
        _LOGGER.warning("In-process resolve of %s exceeded %ss", configuration, timeout)
        return await run_in_executor(_locate, controller._db.settings, configuration), None


async def resolve_config_subprocess(
    controller: DevicesController, path: Path
) -> dict[Any, Any] | None:
    """Resolve through ``esphome config`` alone; ``None`` with no CLI, on a fault, or if invalid."""
    esphome_cmd = controller.state.esphome_cmd
    if not esphome_cmd:
        return None
    try:
        return await run_esphome_config(esphome_cmd, path)
    except EsphomeConfigUnavailableError:
        return None


def _locate(settings: DashboardSettings, configuration: str | Path) -> Path:
    """Return a caller's ``Path`` as is, else resolve the name under the config dir."""
    if isinstance(configuration, Path):
        return configuration
    return settings.rel_path(configuration)


def _locate_and_load(
    settings: DashboardSettings, configuration: str | Path
) -> tuple[Path, dict[Any, Any] | None]:
    path = _locate(settings, configuration)
    return path, load_device_yaml(path)
