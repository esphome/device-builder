"""
A real native ESP-IDF compile round-trips through the offload session (#1102).

The native-IDF toolchain (``esp32: toolchain: esp-idf``) builds into
``build/`` not ``.pioenvs/<name>/``, so it stresses the offloader's
artifact enumeration differently than the LibreTiny e2e. Skipped on
esphome without the toolchain (< 2026.5.0); runs for real on the e2e
CI job's ``dev`` channel. ``timeout(900)`` covers a cold IDF install.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from esphome.storage_json import StorageJSON

from esphome_device_builder.controllers.firmware.download import (
    collect_download_entries,
    get_binaries,
)

from ....conftest import HAS_NATIVE_IDF_TOOLCHAIN
from ...conftest import PairedInstances, run_offload_compile_round_trip

pytestmark = pytest.mark.skipif(
    not HAS_NATIVE_IDF_TOOLCHAIN, reason="esphome lacks the native ESP-IDF toolchain (< 2026.5.0)"
)

_DEVICE = "esp-idf-e2e"
_CONFIGURATION_FILENAME = f"{_DEVICE}.yaml"
_ESP_IDF_YAML = f"""\
esphome:
  name: {_DEVICE}
esp32:
  board: esp32-c3-devkitm-1
  toolchain: esp-idf
  framework:
    type: esp-idf
logger:
""".encode()


def _local_download_set(data_dir: Path) -> set[str]:
    """Return the download filenames a *local* build of this device offers.

    Runs the production selection (:func:`collect_download_entries`)
    against the receiver's own storage + build dir, so the parity check
    shares one source of truth with the offloader side.
    """
    [storage_path] = list((data_dir / "storage").glob("*.json"))
    storage = StorageJSON.load(storage_path)
    assert storage is not None
    return {entry["file"] for entry in collect_download_entries(storage)}


@pytest.mark.timeout(900)
async def test_esp_idf_compile_download_round_trip(
    paired_instances: PairedInstances,
) -> None:
    """A native-IDF compile lands the same downloads offloader-side as a local build (#1102)."""
    data_dir, _build_path = await run_offload_compile_round_trip(
        paired_instances,
        job_id="off-idf-1",
        configuration_filename=_CONFIGURATION_FILENAME,
        yaml_body=_ESP_IDF_YAML,
    )

    # The set a local build of this device would offer for download. Off the
    # loop: ``collect_download_entries`` stats the build dir (blockbuster).
    expected = await asyncio.to_thread(_local_download_set, data_dir)
    assert expected, "compile produced no downloadable artifacts"
    assert "firmware.factory.bin" in expected, expected

    # The offloader's Download picker offers exactly what a local build
    # offers — including build/firmware.elf, which only rides back once
    # BUILD_FILES lists the native-IDF ELF path, and only after the tarball
    # stops requiring platformio.ini / idedata.json (#1102).
    firmware = paired_instances.offloader._db.firmware
    firmware._validate_configuration_boundary = AsyncMock()
    binaries = await get_binaries(firmware, configuration=_CONFIGURATION_FILENAME)
    offered = {entry["file"] for entry in binaries}
    assert offered == expected
