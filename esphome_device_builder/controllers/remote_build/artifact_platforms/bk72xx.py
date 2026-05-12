"""
Libretiny BK72xx (Beken) build-tree files.

Shares the libretiny family's BUILD_FILES; see ``_libretiny.py``
for the rationale and the layout. UART flash on BK72xx uses
``ltchiptool`` via the PIO upload recipe (``pio run -t upload
-t nobuild`` reaches it through ``upload_using_platformio``,
esphome/__main__.py:1155).
"""

from __future__ import annotations

from ._libretiny import BUILD_FILES

__all__ = ["BUILD_FILES", "TARGET_PLATFORM"]

TARGET_PLATFORM = "bk72xx"
