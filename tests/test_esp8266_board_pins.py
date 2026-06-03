"""ESP8266 board-pin derivation from ESPHome's ``ESP8266_BASE_PINS``/``_BOARD_PINS``."""

from __future__ import annotations

from esphome_device_builder.models import PinFeature
from script.sync_boards import _derive_pins_from_aliases, build_catalog


def test_derive_tags_esp8266_base_pins() -> None:
    # ESP8266 fixed-function pins live in the shared base map; derivation tags
    # them by alias even though per-board maps carry only positional names.
    pins = {
        p.gpio: p
        for p in _derive_pins_from_aliases(
            {"A0": 17, "SS": 15, "MOSI": 13, "SCK": 14, "SDA": 4, "SCL": 5, "RX": 3, "TX": 1}
        )
    }
    assert pins[1].features == [PinFeature.UART_TX]
    assert pins[3].features == [PinFeature.UART_RX]
    assert pins[4].features == [PinFeature.I2C_SDA]
    assert pins[5].features == [PinFeature.I2C_SCL]
    assert pins[13].features == [PinFeature.SPI_MOSI]
    assert pins[15].features == [PinFeature.SPI_CS]
    assert pins[17].features == [PinFeature.ADC]
    assert pins[1].notes == "UART TX"


def test_catalog_generates_unmanifested_esp8266_board() -> None:
    boards = {b.id: b for b in build_catalog().boards}
    # huzzah has no device-builder manifest but is in ESPHome's esp8266 BOARDS.
    assert "huzzah" in boards, "huzzah should be auto-generated from ESPHome board data"
    huzzah = boards["huzzah"]
    assert huzzah.esphome.platform.value == "esp8266"
    feats = {f for pin in huzzah.pins for f in pin.features}
    assert PinFeature.UART_TX in feats and PinFeature.I2C_SDA in feats


def test_catalog_does_not_duplicate_manifested_esp8266_board() -> None:
    # The d1-mini manifest is canonical for ``board: d1_mini`` (id normalises to
    # the board), so no second ``d1_mini`` entry is generated.
    ids = [b.id for b in build_catalog().boards if b.esphome.board == "d1_mini"]
    assert "d1-mini" in ids
    assert "d1_mini" not in ids


def test_catalog_generates_canonical_board_only_referenced_by_products() -> None:
    # esp01_1m has no canonical manifest, only product manifests run on it
    # (board: esp01_1m). It must still get a canonical entry so find_by_pio_board
    # has a non-arbitrary target (issue #395).
    boards = {b.id: b for b in build_catalog().boards}
    assert "esp01_1m" in boards
    assert boards["esp01_1m"].esphome.board == "esp01_1m"


def test_catalog_fills_empty_esp8266_product_manifest() -> None:
    boards = {b.id: b for b in build_catalog().boards}
    # mirabella_door_window_sensor (board: esp01_1m) ships ``pins: []``; the base
    # pinout is filled in so the visual editor offers real pins.
    pins = boards["mirabella_door_window_sensor"].pins
    assert pins, "empty-pin product manifest should be filled from ESPHome base pins"
    feats = {f for pin in pins for f in pin.features}
    assert PinFeature.UART_TX in feats
