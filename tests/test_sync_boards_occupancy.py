"""Occupancy-only pin tables land on the chip's full pinout."""

from __future__ import annotations

import pytest

from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardCatalogResponse,
    BoardEsphomeConfig,
    BoardPin,
    Platform,
)
from script.sync_boards import (
    _detach_occupancy_pins,
    _fill_detached_esp32_pins,
    _reattach_occupancy_pins,
)

pytestmark = pytest.mark.xdist_group("board_sync")


def _board(board_id: str, pins: list[BoardPin], *, is_generic: bool = False) -> BoardCatalogEntry:
    return BoardCatalogEntry(
        id=board_id,
        name=board_id,
        description="",
        manufacturer="",
        esphome=BoardEsphomeConfig(platform=Platform.ESP32, board="esp32dev", variant="esp32"),
        pins=pins,
        is_generic=is_generic,
    )


def test_occupied_pins_overlay_the_generic_pinout() -> None:
    generic = _board(
        "generic-esp32",
        [BoardPin(gpio=gpio, label=f"GPIO{gpio}", available=None) for gpio in (4, 21, 22)],
        is_generic=True,
    )
    imported = _board("imported", [BoardPin(gpio=21, available=False, occupied_by="bus_a")])
    boards = [generic, imported]
    detached = _detach_occupancy_pins(boards)
    assert imported.pins == []
    assert "generic-esp32" not in detached
    _fill_detached_esp32_pins(boards, detached)
    _reattach_occupancy_pins(boards, detached)
    by_gpio = {pin.gpio: pin for pin in imported.pins}
    assert sorted(by_gpio) == [4, 21, 22]
    assert by_gpio[21].available is False
    assert by_gpio[21].occupied_by == "bus_a"
    assert by_gpio[21].label == "GPIO21"
    assert by_gpio[4].available is None
    assert generic.pins[1].occupied_by is None


def test_occupied_pins_survive_without_a_pinout() -> None:
    occupied = [BoardPin(gpio=21, available=False, occupied_by="bus_a")]
    imported = _board("imported", list(occupied))
    detached = _detach_occupancy_pins([imported])
    _reattach_occupancy_pins([imported], detached)
    assert imported.pins == occupied


def test_m5stickc_plus_offers_free_pins(generated_board_catalog: BoardCatalogResponse) -> None:
    board = next(b for b in generated_board_catalog.boards if b.id == "m5stack_m5stickc_plus")
    by_gpio = {pin.gpio: pin for pin in board.pins}
    assert by_gpio[21].occupied_by == "bus_a"
    assert by_gpio[22].occupied_by == "bus_a"
    for gpio in (25, 32, 33, 35, 36):
        assert by_gpio[gpio].available is not False


def test_levoit_core_300s_marks_its_uart_on_the_generic_pinout(
    generated_board_catalog: BoardCatalogResponse,
) -> None:
    boards = {b.id: b for b in generated_board_catalog.boards}
    pins = {pin.gpio: pin for pin in boards["levoit_core_300s"].pins}
    assert set(pins) == {pin.gpio for pin in boards["generic-esp32"].pins}
    assert pins[16].occupied_by == "uart"
    assert pins[17].occupied_by == "uart"
