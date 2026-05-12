"""
ESP8266 build-tree files.

Single-image flash: the eboot bootloader is integrated into the
firmware image, so there's no separate ``bootloader.bin`` /
``partitions.bin``. ``esphome upload`` dispatches through
``upload_using_esptool`` for serial flash (esphome/__main__.py:1152)
and through the native API / web_server OTA path for everything
else, both keying off ``CORE.firmware_bin`` which lands at
``.pioenvs/<name>/firmware.bin`` (esphome/core/__init__.py:780).
``firmware.elf`` rides along for crash-dump symbol resolution.
"""

from __future__ import annotations

TARGET_PLATFORM = "esp8266"

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.elf",
)
