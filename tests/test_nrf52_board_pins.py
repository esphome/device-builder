"""nRF52 board-pin generation from ESPHome's ``BOARDS_ZEPHYR`` + ``AIN_TO_GPIO``."""

from __future__ import annotations

import logging

import pytest

from esphome_device_builder.models import BoardCatalogResponse, PinFeature
from script.sync_boards import _derive_nrf52_pins, build_catalog

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
    assert by_id["adafruit_itsybitsy"].esphome.platform.value == "rp2040"
    assert by_id["adafruit_itsybitsy_nrf52840"].esphome.platform.value == "nrf52"


def test_nrf52_id_clash_logs_warning(caplog) -> None:
    # The drop must not be silent: a cross-platform id clash warns so the nightly
    # catalog gate can see it.
    with caplog.at_level(logging.WARNING, logger="sync_boards"):
        build_catalog()
    assert any(
        "adafruit_itsybitsy" in r.message and "shares a catalog id" in r.message
        for r in caplog.records
    )
