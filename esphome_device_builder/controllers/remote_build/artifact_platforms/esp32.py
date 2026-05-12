"""
ESP32 build-tree files (covers Arduino-on-IDF and native ESP-IDF).

Upstream's ``CORE.firmware_bin`` (esphome/core/__init__.py:774)
returns ``.pioenvs/<name>/firmware.bin`` for non-libretiny
non-native-IDF builds; on ESP32 that branch covers both Arduino
(now built as a framework on top of IDF) and pure ESP-IDF. The
multi-image flash (``bootloader.bin`` at ``0x1000``,
``partitions.bin`` at ``0x8000``, ``ota_data_initial.bin`` at
``0xe000``, ``firmware.bin`` at ``0x10000``) lives entirely under
``.pioenvs/<name>/``; esptool reads the offsets from idedata's
``extra.flash_images`` array — the materialiser stages the
idedata cache file so that lookup hits without invoking
``pio run -t idedata`` on the offloader.

The native-IDF feature flag (``KEY_NATIVE_IDF``) would push the
firmware to ``build/<name>.bin`` instead, but device-builder
doesn't currently surface that flag. If upstream lands it, add a
new ``esp32_native_idf.py`` module rather than forking the
inclusion list inside this one.
"""

from __future__ import annotations

TARGET_PLATFORM = "esp32"

# Build-relative paths. ``{name}`` is filled in with
# ``StorageJSON.name`` at pack time. Files that don't exist on
# disk are silently skipped (e.g. a build that didn't emit
# ``ota_data_initial.bin``).
BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.elf",
    ".pioenvs/{name}/bootloader.bin",
    ".pioenvs/{name}/partitions.bin",
    ".pioenvs/{name}/ota_data_initial.bin",
)
