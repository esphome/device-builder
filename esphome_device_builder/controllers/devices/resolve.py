"""Resolve a configuration's YAML in process, spawning ``esphome config`` only as a fallback."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...helpers.async_ import run_in_executor
from ...helpers.device_yaml import (
    EsphomeConfigUnavailableError,
    load_device_yaml,
    package_merge_incomplete,
    run_esphome_config,
)

if TYPE_CHECKING:
    from .controller import DevicesController


async def resolve_config(
    controller: DevicesController, configuration: str
) -> dict[Any, Any] | None:
    """Load in process, ``esphome config`` when packages didn't merge; ``None`` if neither works."""
    config = await load_config(controller, configuration)
    if package_merge_incomplete(config):
        config = await resolve_config_subprocess(controller, configuration)
    return config


async def load_config(controller: DevicesController, configuration: str) -> dict[Any, Any] | None:
    """Load *configuration* through ESPHome's YAML loader in an executor; ``None`` if unparsable."""
    path = controller._db.settings.rel_path(configuration)
    return await run_in_executor(load_device_yaml, path)


async def resolve_config_subprocess(
    controller: DevicesController, configuration: str
) -> dict[Any, Any] | None:
    """Resolve through ``esphome config``; ``None`` without a CLI, on a fault, or if invalid."""
    esphome_cmd = controller.state.esphome_cmd
    if not esphome_cmd:
        return None
    path = controller._db.settings.rel_path(configuration)
    try:
        return await run_esphome_config(esphome_cmd, path)
    except EsphomeConfigUnavailableError:
        return None
