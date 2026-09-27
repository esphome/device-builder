"""The chip of a device on a platform that lumps several chips under one key."""

from __future__ import annotations

from esphome import const

from ...definitions import load_platform_capabilities_index
from ...models.boards import RP2_CANONICAL_PLATFORM, RP2_PLATFORM_ALIASES, normalize_platform
from ..chips import libretiny_family_mcu, normalize_chip_variant
from ._parsing import _resolve_substitutions, _str_or_none, parse_platform_fields

_CHIP_KEYS = (const.CONF_BOARD, "family", const.CONF_VARIANT)


def resolve_chip_mcu(
    config: dict | None,
    yaml_content: str,
    target_platform: str,
    extra_substitutions: dict[str, str] | None = None,
) -> str | None:
    """
    Chip series of a device on a platform that lumps several chips, else ``None``.

    The chip is what the YAML compiles for, read against the snapshot of
    ESPHome's own board tables. Source order: the ``board:``, then the chip a
    board ESPHome does not list has to name itself (LibreTiny ``family:``, rp2
    ``variant:``), then the platform's only chip when it has just one. The
    resolved *config* sees packages; a shallow scan reads the same fields
    from the raw text.
    """
    platform = normalize_platform(target_platform.strip().lower())
    boards = load_platform_capabilities_index().board_mcus.get(platform)
    if not boards:
        return None
    subs = extra_substitutions or {}
    board, family, variant = (
        (_resolve_substitutions(value, subs) or "").strip()
        for value in _chip_fields(config, yaml_content, platform)
    )
    if board in boards:
        return boards[board]
    chips = set(boards.values())
    named = libretiny_family_mcu(family) if family else normalize_chip_variant(variant)
    if named in chips:
        return named
    return next(iter(chips)) if len(chips) == 1 else None


def _chip_fields(config: dict | None, yaml_content: str, platform: str) -> tuple[str, str, str]:
    """``(board, family, variant)`` of the platform block as written, empty where absent."""
    keys = RP2_PLATFORM_ALIASES if platform == RP2_CANONICAL_PLATFORM else (platform,)
    blocks = [config.get(key) for key in keys] if isinstance(config, dict) else []
    block = next((found for found in blocks if isinstance(found, dict)), None)
    if block is None:
        raw_platform, block = parse_platform_fields(yaml_content, _CHIP_KEYS)
        if normalize_platform(raw_platform) != platform:
            return "", "", ""
    board, family, variant = (_str_or_none(block.get(key)) or "" for key in _CHIP_KEYS)
    # ``family:`` is LibreTiny's key; rp2 names its chip with ``variant:``.
    return board, "" if platform == RP2_CANONICAL_PLATFORM else family, variant
