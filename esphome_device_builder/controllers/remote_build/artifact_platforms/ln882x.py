"""
Libretiny LN882x (Lightning) build-tree files.

Shares the libretiny family's BUILD_FILES; see ``_libretiny.py``
for the rationale and the layout.
"""

from __future__ import annotations

from ._libretiny import BUILD_FILES

__all__ = ["BUILD_FILES", "TARGET_PLATFORM"]

TARGET_PLATFORM = "ln882x"
