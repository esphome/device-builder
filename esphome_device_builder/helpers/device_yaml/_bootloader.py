"""The bootloader an nRF52 device builds for, which decides how it is flashed."""

from __future__ import annotations

from esphome import const

from ...definitions import load_platform_capabilities_index
from ...models.boards import Platform
from ._parsing import _resolve_substitutions, _str_or_none, parse_platform_fields

_CONF_BOOTLOADER = "bootloader"
# ESPHome's choice for a board that is not in its table.
_FALLBACK_BOOTLOADER = "mcuboot"
_FIELDS = (const.CONF_BOARD, _CONF_BOOTLOADER)


def resolve_nrf52_bootloader(
    config: dict | None,
    yaml_content: str,
    target_platform: str,
    extra_substitutions: dict[str, str] | None = None,
) -> str | None:
    """
    Resolve the bootloader an nRF52 device's firmware is built for, else ``None``.

    As ESPHome resolves it: the ``bootloader:`` the YAML names, else the
    board's default from the snapshot of ESPHome's board table, else mcuboot.
    ``None`` off nRF52, or without a readable ``nrf52:`` block.
    """
    if target_platform.strip().lower() != Platform.NRF52:
        return None
    fields = _nrf52_fields(config, yaml_content)
    if fields is None:
        return None
    subs = extra_substitutions or {}
    board, bootloader = ((_resolve_substitutions(value, subs) or "").strip() for value in fields)
    if bootloader:
        return bootloader.lower()
    defaults = load_platform_capabilities_index().nrf52_board_bootloaders
    return defaults.get(board, _FALLBACK_BOOTLOADER)


def _nrf52_fields(config: dict | None, yaml_content: str) -> tuple[str, str] | None:
    """
    ``(board, bootloader)`` of the ``nrf52:`` block as written; ``None`` without one.

    The resolved *config* sees packages; a shallow scan reads the same fields
    from the raw text.
    """
    block = config.get(Platform.NRF52) if isinstance(config, dict) else None
    if not isinstance(block, dict):
        raw_platform, block = parse_platform_fields(yaml_content, _FIELDS)
        if raw_platform != Platform.NRF52:
            return None
    board, bootloader = (_str_or_none(block.get(key)) or "" for key in _FIELDS)
    return board, bootloader
