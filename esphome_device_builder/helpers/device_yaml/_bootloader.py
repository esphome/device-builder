"""The bootloader an nRF52 device builds for, which decides how it is flashed."""

from __future__ import annotations

from esphome import const

from ...definitions import load_platform_capabilities_index
from ...models.boards import normalize_platform
from ._parsing import _resolve_substitutions, _str_or_none, parse_platform_fields

_NRF52 = "nrf52"
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
    The resolved *config* sees packages; a shallow scan reads the same fields
    from the raw text. ``None`` where the platform is not nRF52 or the YAML
    has no ``nrf52:`` block to read.
    """
    if normalize_platform(target_platform.strip().lower()) != _NRF52:
        return None
    fields = _nrf52_fields(config, yaml_content)
    if fields is None:
        return None
    subs = extra_substitutions or {}
    board, bootloader = (
        (_resolve_substitutions(value, subs) or "").strip().lower() for value in fields
    )
    if bootloader:
        return bootloader
    defaults = load_platform_capabilities_index().nrf52_board_bootloaders
    return defaults.get(board, _FALLBACK_BOOTLOADER)


def _nrf52_fields(config: dict | None, yaml_content: str) -> tuple[str, str] | None:
    """``(board, bootloader)`` of the ``nrf52:`` block as written; ``None`` without one."""
    block = config.get(_NRF52) if isinstance(config, dict) else None
    if isinstance(block, dict):
        return tuple(_str_or_none(block.get(key)) or "" for key in _FIELDS)  # type: ignore[return-value]
    raw_platform, raw = parse_platform_fields(yaml_content, _FIELDS)
    if normalize_platform(raw_platform) != _NRF52:
        return None
    return raw[const.CONF_BOARD], raw[_CONF_BOOTLOADER]
