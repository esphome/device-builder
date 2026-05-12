"""RP2040 build-tree files (BOOTSEL via picotool + .uf2; serial via PIO)."""

from __future__ import annotations

TARGET_PLATFORM = "rp2040"

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.uf2",
    ".pioenvs/{name}/firmware.elf",
)
