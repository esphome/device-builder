"""ESP8266 build-tree files (eboot bootloader is in the firmware image — single flash)."""

from __future__ import annotations

TARGET_PLATFORM = "esp8266"

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.elf",
)
