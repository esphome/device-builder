"""ESP32 build-tree files (covers Arduino-on-IDF and native ESP-IDF)."""

from __future__ import annotations

TARGET_PLATFORM = "esp32"

# Basenames under <build_path>/.pioenvs/<name>/. Files that don't
# exist on disk are silently skipped.
BUILD_FILES: tuple[str, ...] = (
    "firmware.bin",
    "firmware.elf",
    "bootloader.bin",
    "partitions.bin",
    "ota_data_initial.bin",
)
