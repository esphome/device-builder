"""nRF52 (Zephyr) build-tree files (BLE OTA reads zephyr/app_update.bin)."""

from __future__ import annotations

TARGET_PLATFORM = "nrf52"

BUILD_FILES: tuple[str, ...] = (
    "firmware.bin",
    "firmware.elf",
    "zephyr/app_update.bin",
    "zephyr/zephyr.elf",
)
