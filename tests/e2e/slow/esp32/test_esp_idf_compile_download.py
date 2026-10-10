"""
A real native ESP-IDF compile round-trips through the offload session (#1102).

The native-IDF toolchain (``esp32: toolchain: esp-idf``) builds into
``build/`` not ``.pioenvs/<name>/``, so it stresses the offloader's
artifact enumeration differently than the LibreTiny e2e. Runs for real
on the e2e CI job's ``dev`` channel. ``timeout(900)`` covers a cold IDF
install.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.firmware.download import get_binaries

from ...conftest import PairedInstances, local_download_set, run_offload_compile_round_trip

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


@pytest.mark.timeout(900)
async def test_esp_idf_compile_download_round_trip(
    paired_instances: PairedInstances,
) -> None:
    """A native-IDF compile lands the same downloads offloader-side as a local build (#1102)."""
    data_dir, build_path = await run_offload_compile_round_trip(
        paired_instances,
        job_id="off-idf-1",
        configuration_filename=_CONFIGURATION_FILENAME,
        yaml_body=_ESP_IDF_YAML,
    )

    # The native bootloader/partition set rides back for OTA
    # bootloader / partition-table updates.
    native_flash_files = [
        build_path / "build" / "bootloader" / "bootloader.bin",
        build_path / "build" / "partition_table" / "partition-table.bin",
        build_path / "build" / "ota_data_initial.bin",
    ]
    missing = await asyncio.to_thread(
        lambda: [str(p) for p in native_flash_files if not p.is_file()]
    )
    assert not missing, f"native flash files not materialised: {missing}"

    # The set a local build of this device would offer for download. Off the
    # loop: ``collect_download_entries`` stats the build dir (blockbuster).
    expected = await asyncio.to_thread(local_download_set, data_dir, _CONFIGURATION_FILENAME)
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
