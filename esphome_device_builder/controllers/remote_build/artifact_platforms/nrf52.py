"""nRF52 (Zephyr) build-tree files (BLE OTA reads zephyr/app_update.bin)."""

from __future__ import annotations

TARGET_PLATFORM = "nrf52"

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.elf",
    ".pioenvs/{name}/zephyr/app_update.bin",
    ".pioenvs/{name}/zephyr/zephyr.elf",
)
