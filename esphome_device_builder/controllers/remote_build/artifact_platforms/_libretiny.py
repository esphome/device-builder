"""
Shared BUILD_FILES for the libretiny family (bk72xx / rtl87xx / ln882x).

Upstream's libretiny PlatformIO platform writes the same on-disk
layout for every variant in the family, so the per-platform modules
``bk72xx.py`` / ``rtl87xx.py`` / ``ln882x.py`` all re-export this
tuple. Splitting into three tiny modules keeps the registry walk
loop-level uniform (one module per ``target_platform`` value) while
avoiding the maintenance cost of three identical copies.

Esphome's ``CORE.firmware_bin`` for libretiny returns
``.pioenvs/<name>/firmware.uf2`` (esphome/core/__init__.py:778),
which is the artefact ``ltchiptool`` flashes over UART. The same
build also emits ``firmware.bin`` for the native API / web_server
OTA paths plus ``firmware.elf`` for symbol resolution; we ship all
three so every install path resolves cleanly against the staged
tree.
"""

from __future__ import annotations

BUILD_FILES: tuple[str, ...] = (
    ".pioenvs/{name}/firmware.uf2",
    ".pioenvs/{name}/firmware.bin",
    ".pioenvs/{name}/firmware.elf",
)
