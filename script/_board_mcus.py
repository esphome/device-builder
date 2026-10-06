"""The board → chip table of the platforms that lump several chips under one key.

``sync_components.py`` snapshots it for the running dashboard (a device's
``mcu``) and ``sync_boards.py`` stamps the catalog's boards from it, so the
two can never name different chips for one board. Imports
``esphome.components``, which only the sync scripts may do.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from esphome_device_builder.helpers.chips import libretiny_family_mcu  # noqa: E402
from esphome_device_builder.models.boards import RP2_CANONICAL_PLATFORM  # noqa: E402

# ESPHome reads a board with no ``mcu`` as the original chip.
_RP2_DEFAULT_MCU = "rp2040"


def board_mcus() -> dict[str, dict[str, str]]:
    """``{platform: {pio_board: mcu}}`` from the installed ESPHome's board tables."""
    from esphome.components.libretiny import _RENAMED_BOARDS
    from esphome.components.libretiny.const import FAMILY_COMPONENT
    from esphome.components.rp2.boards import BOARDS as RP2_BOARDS

    table: dict[str, dict[str, str]] = {
        RP2_CANONICAL_PLATFORM: {
            board: str(info.get("mcu", _RP2_DEFAULT_MCU)) for board, info in RP2_BOARDS.items()
        }
    }
    for platform in sorted(set(FAMILY_COMPONENT.values())):
        module = importlib.import_module(f"esphome.components.{platform}.boards")
        boards: dict[str, Any] = getattr(module, f"{platform.upper()}_BOARDS")
        chips = {
            board: libretiny_family_mcu(info["family"])
            for board, info in boards.items()
            if isinstance(info.get("family"), str)
        }
        # A YAML may still name a board by the id it had before a rename.
        chips.update({old: chips[new] for old, new in _RENAMED_BOARDS.items() if new in chips})
        table[platform] = chips
    return {platform: dict(sorted(chips.items())) for platform, chips in table.items()}


def catalog_board_mcu(table: dict[str, dict[str, str]], platform: str, board: str) -> str | None:
    """
    Chip of a catalog board on *platform*, ``None`` where it needs no split.

    A board ESPHome does not list takes the platform's only chip when it has
    one, and rp2's original chip.
    """
    chips = table.get(platform)
    if chips is None:
        return None
    if board in chips:
        return chips[board]
    if platform == RP2_CANONICAL_PLATFORM:
        return _RP2_DEFAULT_MCU
    distinct = set(chips.values())
    return next(iter(distinct)) if len(distinct) == 1 else None
