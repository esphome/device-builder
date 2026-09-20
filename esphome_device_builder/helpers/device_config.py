"""Read a device config by name, answering ``NOT_FOUND`` when it is missing."""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from ..models import ErrorCode
from .api import CommandError
from .async_ import run_in_executor

if TYPE_CHECKING:
    from pathlib import Path

    from ..controllers.config.settings import DashboardSettings


def raise_device_not_found(
    configuration: str, *, from_exc: BaseException | None = None
) -> NoReturn:
    """Raise ``NOT_FOUND`` for a missing device *configuration*."""
    err = CommandError(ErrorCode.NOT_FOUND, f"Device {configuration!r} not found")
    if from_exc is not None:
        raise err from from_exc
    raise err


def read_device_config(path: Path, configuration: str) -> str:
    """Read *configuration*'s YAML at *path*; ``NOT_FOUND`` when it is missing. Blocking."""
    try:
        return path.read_text("utf-8")
    except FileNotFoundError as err:
        raise_device_not_found(configuration, from_exc=err)


async def read_device_config_async(settings: DashboardSettings, configuration: str) -> str:
    """Read *configuration*'s YAML off the loop; ``NOT_FOUND`` when it is missing."""
    return await run_in_executor(
        read_device_config, settings.rel_path(configuration), configuration
    )
