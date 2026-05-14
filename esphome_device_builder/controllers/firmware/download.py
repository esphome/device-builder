"""Firmware-binary discovery + download endpoints."""

from __future__ import annotations

import asyncio
import base64
import gzip
import importlib
import logging
from typing import TYPE_CHECKING

from esphome.components.esp32 import VARIANTS as ESP32_VARIANTS
from esphome.components.libretiny.const import (
    FAMILY_COMPONENT as _LIBRETINY_FAMILY_COMPONENT,
)
from esphome.storage_json import StorageJSON

from ...helpers.storage_path import resolve_storage_path

if TYPE_CHECKING:
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)


# Platforms whose ``target_platform`` value isn't the component
# module name. ESP32 variants collapse to the umbrella ``esp32``
# component; LibreTiny chip families collapse to ``libretiny``.
# The LibreTiny set is sourced from upstream's
# ``FAMILY_COMPONENT.values()`` so it picks up new chip families
# automatically on the next ``esphome`` dependency bump.
_LIBRETINY_TARGET_PLATFORMS: frozenset[str] = frozenset(_LIBRETINY_FAMILY_COMPONENT.values()) | {
    "libretiny"
}


async def get_binaries(controller: FirmwareController, *, configuration: str) -> list[dict]:
    """List firmware binaries for a compiled device as ``[{title, file}]``.

    The ``file`` names can be passed to ``firmware/download`` to
    retrieve the binary content.
    """
    # ``resolve_storage_path`` collapses to
    # ``<data_dir>/storage/<Path(configuration).name>.json``; a
    # traversal-shaped *configuration* could still escape to an
    # attacker-controlled basename inside the storage tree, so the
    # validator below is the gate. Do not reorder.
    await controller._validate_configuration_boundary(configuration)
    loop = asyncio.get_running_loop()

    def _get_types() -> list[dict]:
        storage = StorageJSON.load(resolve_storage_path(configuration))
        if storage is None:
            return []
        try:
            component = _resolve_download_component(storage.target_platform)
            module = importlib.import_module(f"esphome.components.{component}")
            return list(module.get_download_types(storage))
        except Exception:  # noqa: BLE001 — third-party regression: upstream ``get_download_types`` could raise anything
            _LOGGER.warning("Could not determine download types for %s", configuration)
            return []

    return await loop.run_in_executor(None, _get_types)


async def download(
    controller: FirmwareController,
    *,
    configuration: str,
    file: str,
    compressed: bool = False,
) -> dict:
    """Download a compiled firmware binary as ``{filename, data, size, compressed}``.

    ``data`` is base64-encoded bytes; for Web Serial flashing the
    frontend decodes the base64 itself.
    """
    # ``_validate_configuration_boundary`` is the only traversal
    # gate; do not reorder. Coverage:
    # ``test_download.py::test_download_validator_runs_before_ext_storage_path``.
    await controller._validate_configuration_boundary(configuration)
    loop = asyncio.get_running_loop()

    def _read_binary() -> dict:
        storage = StorageJSON.load(resolve_storage_path(configuration))
        if storage is None or storage.firmware_bin_path is None:
            msg = "No firmware binary — compile the device first"
            raise FileNotFoundError(msg)

        base_dir = storage.firmware_bin_path.parent.resolve()
        path = (base_dir / file).resolve()
        # Path traversal protection
        path.relative_to(base_dir)

        if not path.is_file():
            msg = f"Binary not found: {file}"
            raise FileNotFoundError(msg)

        data = path.read_bytes()
        if compressed:
            data = gzip.compress(data, 9)

        filename = f"{storage.name}-{file}"
        if compressed:
            filename += ".gz"

        return {
            "filename": filename,
            "data": base64.b64encode(data).decode("ascii"),
            "size": len(data),
            "compressed": compressed,
        }

    return await loop.run_in_executor(None, _read_binary)


def _resolve_download_component(target_platform: str | None) -> str:
    """Return the ``esphome.components`` module name for *target_platform*.

    ``None`` / empty input collapses to ``""``; the caller's
    ``importlib.import_module`` then fails in its ``try/except``
    and logs a warning.
    """
    platform = (target_platform or "").lower()
    if platform.upper() in ESP32_VARIANTS:
        return "esp32"
    if platform in _LIBRETINY_TARGET_PLATFORMS:
        return "libretiny"
    return platform
