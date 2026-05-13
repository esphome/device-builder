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
# module name. The dashboard download endpoint needs the
# ``esphome.components.<X>`` module that exposes
# ``get_download_types(storage)`` — for ESP32 variants that's the
# umbrella ``esp32`` component, and for LibreTiny chip families it's
# the ``libretiny`` component.
#
# The LibreTiny set is derived from upstream's
# ``FAMILY_COMPONENT.values()`` (auto-generated from
# ``generate_components.py``) so when LibreTiny adds a new chip
# family / component our mapping picks it up on the next
# ``esphome`` dependency bump — no edit here. The literal
# ``"libretiny"`` covers configs that report the umbrella name as
# ``target_platform`` directly.
#
# Mirrors ``esphome/dashboard/web_server.py``'s
# ``DownloadListRequestHandler`` — same shape, but driven by an
# upstream-sourced set rather than an inline literal.
_LIBRETINY_TARGET_PLATFORMS: frozenset[str] = frozenset(_LIBRETINY_FAMILY_COMPONENT.values()) | {
    "libretiny"
}


def _resolve_download_component(target_platform: str | None) -> str:
    """Return the ``esphome.components`` module name for *target_platform*.

    Accepts ``None`` so callers can pass ``StorageJSON.target_platform``
    (which is itself nullable) without an explicit ``or ""``
    coercion at the call site. Returns the empty string for empty
    / missing input — the caller's ``importlib.import_module`` will
    fail in its ``try/except`` block and log a warning.

    See ``_LIBRETINY_TARGET_PLATFORMS`` for the keep-in-sync note.
    """
    platform = (target_platform or "").lower()
    if platform.upper() in ESP32_VARIANTS:
        return "esp32"
    if platform in _LIBRETINY_TARGET_PLATFORMS:
        return "libretiny"
    return platform


async def get_binaries(controller: FirmwareController, *, configuration: str) -> list[dict]:
    """
    List available firmware binaries for a compiled device.

    Returns ``[{title, file}]`` — the file names can be passed to
    ``firmware/download`` to retrieve the binary content.
    """
    # ``resolve_storage_path`` collapses to
    # ``<data_dir>/storage/<Path(configuration).name>.json`` —
    # the basename collapse defangs separators in the
    # configuration but a traversal-shaped *configuration*
    # would still escape the config dir before reaching the
    # closure (e.g. opening a sidecar at an attacker-controlled
    # path under ``<data_dir>/storage``). The validator below
    # is the gate that keeps any traversal payload out of the
    # inner closure entirely. Do not reorder.
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
        except Exception:
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
    """
    Download a compiled firmware binary.

    Returns ``{filename, data, size, compressed}`` where ``data`` is
    base64-encoded bytes. For Web Serial flashing the frontend
    decodes the base64 itself.
    """
    # See ``get_binaries`` — ``resolve_storage_path`` collapses
    # to ``<data_dir>/storage/<Path(configuration).name>.json``,
    # but a traversal-shaped *configuration* could still resolve
    # to an attacker-controlled basename inside the storage
    # tree (e.g. by stripping segments down to a sensitive
    # leaf), so we re-validate at the WS boundary.
    # ``_validate_configuration_boundary`` is the only gate;
    # do not reorder. Coverage:
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
