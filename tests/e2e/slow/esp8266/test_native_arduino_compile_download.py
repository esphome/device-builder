"""
A real native ESP8266 Arduino compile round-trips through the offload session.

The native Arduino toolchain (``esp8266: toolchain: arduino``, the default since
esphome 2026.10) keeps the ``.pioenvs/<name>/`` tree but writes no
``platformio.ini`` and keys its idedata cache as ``<name>.arduino.json``, so
the pack, the materialise and the offloader's cache lookup all have to follow
that layout. ``timeout(900)`` covers a cold toolchain download.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.firmware.download import get_binaries
from esphome_device_builder.helpers.storage_path import resolve_idedata_path

from ...conftest import PairedInstances, local_download_set, run_offload_compile_round_trip

_DEVICE = "esp8266-arduino-e2e"
_CONFIGURATION_FILENAME = f"{_DEVICE}.yaml"
_NATIVE_ARDUINO_YAML = f"""\
esphome:
  name: {_DEVICE}
esp8266:
  board: esp01_1m
  toolchain: arduino
logger:
""".encode()


@pytest.mark.timeout(900)
async def test_native_arduino_compile_download_round_trip(
    paired_instances: PairedInstances,
) -> None:
    """A native Arduino compile lands its images and ``<name>.arduino.json`` offloader-side."""
    data_dir, build_path = await run_offload_compile_round_trip(
        paired_instances,
        job_id="off-8266-1",
        configuration_filename=_CONFIGURATION_FILENAME,
        yaml_body=_NATIVE_ARDUINO_YAML,
    )

    # ESP8266 flashes one image; the ELF rides along for backtrace decoding.
    pioenvs = build_path / ".pioenvs" / _DEVICE
    images = [pioenvs / "firmware.bin", pioenvs / "firmware.elf"]
    missing = await asyncio.to_thread(lambda: [str(p) for p in images if not p.is_file()])
    assert not missing, f"native Arduino images not materialised: {missing}"
    assert not await asyncio.to_thread((build_path / "platformio.ini").exists)

    # The staged cache lands under the native Arduino name, where esphome's
    # own arduino8266 toolchain and the backtrace decoder read it.
    cached = resolve_idedata_path(_CONFIGURATION_FILENAME, name=_DEVICE, toolchain="arduino")
    assert await asyncio.to_thread(cached.is_file), cached

    expected = await asyncio.to_thread(local_download_set, data_dir, _CONFIGURATION_FILENAME)
    assert "firmware.bin" in expected, expected

    firmware = paired_instances.offloader._db.firmware
    firmware._validate_configuration_boundary = AsyncMock()
    binaries = await get_binaries(firmware, configuration=_CONFIGURATION_FILENAME)
    offered = {entry["file"] for entry in binaries}
    assert offered == expected
