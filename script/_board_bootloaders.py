"""The board → default bootloader table of the nRF52 platform.

``sync_components.py`` snapshots it for the running dashboard (a device's
``bootloader``), which must not import ``esphome.components`` itself. Only the
sync scripts may.
"""

from __future__ import annotations


def nrf52_board_bootloaders() -> dict[str, str]:
    """
    ``{pio_board: bootloader}`` from the installed ESPHome's nRF52 board table.

    The bootloader is the first of the board's list, which ESPHome takes when
    the YAML names none; a board without a list builds for mcuboot and is
    left out, as the dashboard falls back to that itself.
    """
    from esphome.components.nrf52.boards import BOARDS_ZEPHYR
    from esphome.components.zephyr.const import KEY_BOOTLOADER

    return {
        board: str(info[KEY_BOOTLOADER][0])
        for board, info in sorted(BOARDS_ZEPHYR.items())
        if isinstance(info.get(KEY_BOOTLOADER), list) and info[KEY_BOOTLOADER]
    }
