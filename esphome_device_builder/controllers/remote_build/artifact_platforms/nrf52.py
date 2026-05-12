"""nRF52 (Zephyr) build-tree files (BLE OTA reads zephyr/app_update.bin)."""

from __future__ import annotations

TARGET_PLATFORM = "nrf52"

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.elf",
    # ``get_download_types`` for nRF52 lists zephyr.uf2 +
    # firmware.zip when the UF2 build is present, or zephyr.hex
    # / merged.hex + app_update.bin for the SWD/BLE-OTA path.
    ".pioenvs/{name}/zephyr/zephyr.uf2",
    ".pioenvs/{name}/firmware.zip",
    ".pioenvs/{name}/zephyr/zephyr.hex",
    ".pioenvs/{name}/zephyr/merged.hex",
    ".pioenvs/{name}/zephyr/app_update.bin",
    ".pioenvs/{name}/zephyr/zephyr.elf",
)
