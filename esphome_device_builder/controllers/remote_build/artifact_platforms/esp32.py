"""ESP32 build-tree files (covers Arduino-on-IDF and native ESP-IDF)."""

from __future__ import annotations

TARGET_PLATFORM = "esp32"

# Build-relative paths. ``{name}`` substitutes ``StorageJSON.name``
# at pack time. Files missing on disk are silently skipped. Paths
# outside ``.pioenvs/<name>/`` are fine — native ESP-IDF's
# ``build/firmware.factory.bin`` would land here without
# special-casing if/when device-builder surfaces it.
BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.elf",
    # ``firmware.factory.bin`` + ``firmware.ota.bin`` are the
    # download options esphome's ``get_download_types`` lists
    # for ESP32.
    ".pioenvs/{name}/firmware.factory.bin",
    ".pioenvs/{name}/firmware.ota.bin",
    ".pioenvs/{name}/bootloader.bin",
    ".pioenvs/{name}/partitions.bin",
    ".pioenvs/{name}/ota_data_initial.bin",
    # Native ESP-IDF (non-PIO) layout — only present when the
    # build went through ``KEY_NATIVE_IDF``; harmlessly skipped
    # otherwise. ``build/firmware.elf`` is the native-IDF ELF; the
    # ``.pioenvs`` copy above wins the basename dedupe when both exist.
    "build/firmware.factory.bin",
    "build/{name}.bin",
    "build/firmware.elf",
)
