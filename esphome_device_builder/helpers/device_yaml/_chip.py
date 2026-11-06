"""The chip and board a device compiles for, read from its platform block."""

from __future__ import annotations

from collections.abc import Callable

from esphome import const

from ...definitions import load_platform_capabilities_index
from ...models.boards import (
    RP2_CANONICAL_PLATFORM,
    RP2_PLATFORM_ALIASES,
    Platform,
    normalize_platform,
)
from ..chips import libretiny_family_mcu, normalize_chip_variant
from ._parsing import (
    _resolve_substitutions,
    _str_or_none,
    parse_platform_fields,
    resolve_esp32_variant,
)

_CHIP_KEYS = (const.CONF_BOARD, "family", const.CONF_VARIANT)

# Maps a device's ``(platform, board, variant)`` to a board catalog id, ``""`` on a miss.
BoardIdDeriver = Callable[[str, str, str], str]


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
    board, family, variant = _platform_block_values(
        config, yaml_content, platform, _CHIP_KEYS, extra_substitutions
    )
    if board in boards:
        return boards[board]
    chips = set(boards.values())
    # ``family:`` is LibreTiny's key; rp2 names its chip with ``variant:``.
    if family and platform != RP2_CANONICAL_PLATFORM:
        named = libretiny_family_mcu(family)
    else:
        named = normalize_chip_variant(variant)
    if named in chips:
        return named
    return next(iter(chips)) if len(chips) == 1 else None


def resolve_board_fields(
    config: dict | None,
    yaml_content: str,
    target_platform: str,
    extra_substitutions: dict[str, str] | None = None,
    storage_variant: str | None = None,
) -> tuple[str, str]:
    """
    ``(board, variant)`` the device compiles for, empty where unknown.

    An esp32 chip follows :func:`resolve_esp32_variant`, the rule the logger interface uses.
    """
    platform = normalize_platform(target_platform.strip().lower())
    board, variant = _platform_block_values(
        config, yaml_content, platform, (const.CONF_BOARD, const.CONF_VARIANT), extra_substitutions
    )
    if platform == Platform.ESP32:
        block = {const.CONF_BOARD: board, const.CONF_VARIANT: variant}
        variant = resolve_esp32_variant({platform: block}, storage_variant=storage_variant) or ""
    return board, variant


def derive_device_board_id(
    derive_board_id: BoardIdDeriver | None,
    config: dict | None,
    yaml_content: str,
    target_platform: str,
    extra_substitutions: dict[str, str],
    storage_variant: str | None,
) -> str:
    """Catalog id for the board the device compiles for; ``""`` with no *derive_board_id*."""
    if derive_board_id is None:
        return ""
    board, variant = resolve_board_fields(
        config, yaml_content, target_platform, extra_substitutions, storage_variant
    )
    return derive_board_id(target_platform, board, variant)


def _platform_block_values(
    config: dict | None,
    yaml_content: str,
    platform: str,
    keys: tuple[str, ...],
    extra_substitutions: dict[str, str] | None,
) -> list[str]:
    """Values of *keys* in the *platform* block, substitutions resolved, empty where absent."""
    aliases = RP2_PLATFORM_ALIASES if platform == RP2_CANONICAL_PLATFORM else (platform,)
    blocks = [config.get(key) for key in aliases] if isinstance(config, dict) else []
    block = next((found for found in blocks if isinstance(found, dict)), None)
    if block is None:
        raw_platform, block = parse_platform_fields(yaml_content, keys)
        if normalize_platform(raw_platform) != platform:
            return [""] * len(keys)
    subs = extra_substitutions or {}
    values = (_resolve_substitutions(_str_or_none(block.get(key)), subs) for key in keys)
    return [(value or "").strip() for value in values]
