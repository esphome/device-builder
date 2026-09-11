"""Resolve a configuration's YAML in process, spawning ``esphome config`` only as a fallback."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import (
    EsphomeConfigUnavailableError,
    load_device_yaml,
    resolution_incomplete,
    run_esphome_config,
)

if TYPE_CHECKING:
    from .controller import DevicesController


async def resolve_config(
    controller: DevicesController, configuration: str | Path
) -> dict[Any, Any] | None:
    """Load in process, then ``esphome config`` if work was deferred; ``None`` if neither works."""
    path = await _config_path(controller, configuration)
    config = await run_in_executor(load_device_yaml, path)
    if resolution_incomplete(config):
        config = await _resolve_subprocess(controller, path)
    return config


async def load_config(
    controller: DevicesController, configuration: str | Path
) -> dict[Any, Any] | None:
    """Load through ESPHome's YAML loader in an executor, no subprocess; ``None`` if unparsable."""
    return await run_in_executor(load_device_yaml, await _config_path(controller, configuration))


async def resolve_config_subprocess(
    controller: DevicesController, configuration: str | Path
) -> dict[Any, Any] | None:
    """Resolve through ``esphome config`` alone; ``None`` with no CLI, on a fault, or if invalid."""
    return await _resolve_subprocess(controller, await _config_path(controller, configuration))


async def _config_path(controller: DevicesController, configuration: str | Path) -> Path:
    """Return a caller's ``Path`` as is, else resolve the name under the config dir off-loop."""
    if isinstance(configuration, Path):
        return configuration
    return await run_in_executor(controller._db.settings.rel_path, configuration)


async def _resolve_subprocess(controller: DevicesController, path: Path) -> dict[Any, Any] | None:
    esphome_cmd = controller.state.esphome_cmd
    if not esphome_cmd:
        return None
    try:
        return await run_esphome_config(esphome_cmd, path)
    except EsphomeConfigUnavailableError:
        return None
