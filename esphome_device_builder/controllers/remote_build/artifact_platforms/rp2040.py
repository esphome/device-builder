"""RP2040 build-tree files (BOOTSEL via picotool + .uf2; serial via PIO)."""

from __future__ import annotations

from ....models.boards import RP2_CANONICAL_PLATFORM

TARGET_PLATFORM = RP2_CANONICAL_PLATFORM

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.uf2",
    # ``get_download_types`` lists firmware.uf2 + firmware.ota.bin.
    ".pioenvs/{name}/firmware.ota.bin",
    ".pioenvs/{name}/firmware.elf",
)
