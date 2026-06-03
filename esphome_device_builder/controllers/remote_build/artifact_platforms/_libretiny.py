"""Shared BUILD_FILES for the libretiny family (bk72xx / rtl87xx / ln882x)."""

from __future__ import annotations

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.uf2",
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.elf",
    # libretiny's get_download_types reads firmware.json to enumerate the
    # public chip images (Beken .rbl, cloudcutter .ug.bin); without it the
    # offloader's Download picker offers only firmware.uf2 (#1102).
    ".pioenvs/{name}/firmware.json",
)
