"""nRF52 board-pin generation from ESPHome's ``BOARDS_ZEPHYR`` + ``AIN_TO_GPIO``."""

from __future__ import annotations

import logging

import pytest

from esphome_device_builder.models import BoardCatalogEntry, BoardCatalogResponse, PinFeature
from script.sync_boards import _NRF52_BOARD_NAMES, _augment_nrf52_boards, _derive_nrf52_pins

pytestmark = pytest.mark.xdist_group("board_sync")


def test_derive_nrf52_pins_tags_adc_and_labels_port_pin() -> None:
    pins = _derive_nrf52_pins({2, 28})
    assert len(pins) == 49  # P0.0 .. P1.16
    by_gpio = {p.gpio: p for p in pins}
    assert by_gpio[2].features == [PinFeature.ADC]
    assert by_gpio[2].notes == "ADC"
    assert by_gpio[28].features == [PinFeature.ADC]
    assert by_gpio[0].features == []
    assert by_gpio[0].notes is None
    # P{port}.{pin} = port*32 + pin — the form ESPHome's nRF52 validator accepts.
    assert by_gpio[27].label == "P0.27"
    assert by_gpio[33].label == "P1.1"
    assert by_gpio[48].label == "P1.16"


def test_catalog_generates_nrf52_boards(
    generated_board_catalog: BoardCatalogResponse,
) -> None:
    boards = {b.esphome.board: b for b in generated_board_catalog.boards}
    assert "xiao_ble" in boards, "xiao_ble should be auto-generated from ESPHome board data"
    xiao = boards["xiao_ble"]
    assert xiao.esphome.platform.value == "nrf52"
    assert xiao.name == "Seeed XIAO nRF52840"
    assert xiao.pins, "generated board should carry pins"
    adc_pins = [p for p in xiao.pins if PinFeature.ADC in p.features]
    assert adc_pins, "ADC-capable pins should be tagged"


def test_nrf52_does_not_steal_rp2040_itsybitsy(
    generated_board_catalog: BoardCatalogResponse,
) -> None:
    # `adafruit_itsybitsy` is a board id on both rp2040 and nRF52 (the nRF52 one
    # is the legacy alias of adafruit_itsybitsy_nrf52840). An id-keyed catalog
    # can't serve both, so the clash leaves rp2040's entry in place rather than
    # shadowing it onto nRF52 pins.
    by_id = {b.id: b for b in generated_board_catalog.boards}
    assert by_id["adafruit_itsybitsy"].esphome.platform.value == "rp2"
    assert by_id["adafruit_itsybitsy_nrf52840"].esphome.platform.value == "nrf52"


def test_nrf52_id_clash_logs_warning(
    generated_board_catalog_with_warnings: tuple[BoardCatalogResponse, list[logging.LogRecord]],
) -> None:
    # The drop must not be silent: a cross-platform id clash warns so the nightly
    # catalog gate can see it.
    _, records = generated_board_catalog_with_warnings
    assert any(
        "adafruit_itsybitsy" in r.getMessage() and "shares a catalog id" in r.getMessage()
        for r in records
    )


def test_nrf52_hwmv2_qualified_name_flattens_id_but_not_board_or_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pinned ESPHome has no '/'-qualified BOARDS_ZEPHYR entry yet.
    import esphome.components.nrf52.boards as boards_module  # noqa: PLC0415

    monkeypatch.setattr(boards_module, "BOARDS_ZEPHYR", {"adafruit_itsybitsy/nrf52840": {}})
    boards: list[BoardCatalogEntry] = []
    _augment_nrf52_boards(boards)
    assert len(boards) == 1
    board = boards[0]
    assert board.id == "adafruit_itsybitsy_nrf52840"
    assert board.esphome.board == "adafruit_itsybitsy/nrf52840"
    assert board.name == _NRF52_BOARD_NAMES["adafruit_itsybitsy_nrf52840"]


def test_nrf52_flatten_collision_keeps_first_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A legacy flat key and its '/'-qualified spelling collapse to one catalog id.
    import esphome.components.nrf52.boards as boards_module  # noqa: PLC0415

    monkeypatch.setattr(
        boards_module,
        "BOARDS_ZEPHYR",
        {"adafruit_itsybitsy_nrf52840": {}, "adafruit_itsybitsy/nrf52840": {}},
    )
    boards: list[BoardCatalogEntry] = []
    with caplog.at_level(logging.WARNING, logger="script.sync_boards"):
        _augment_nrf52_boards(boards)
    assert len(boards) == 1
    assert boards[0].esphome.board == "adafruit_itsybitsy_nrf52840"
    assert any(
        "flatten to the same catalog id" in r.getMessage()
        and "adafruit_itsybitsy/nrf52840" in r.getMessage()
        for r in caplog.records
    )
