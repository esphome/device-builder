"""Shared BUILD_FILES for the libretiny family (bk72xx / rtl87xx / ln882x)."""

from __future__ import annotations

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.uf2",
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.elf",
)
