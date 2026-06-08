"""ESP32 board generation from ESPHome's ``BOARDS`` / ``ESP32_BOARD_PINS``."""

from __future__ import annotations

import importlib

import pytest

from esphome_device_builder.models import (
    BoardCatalogResponse,
    BoardPin,
    Esp32Variant,
    PinFeature,
)
from script.sync_boards import (
    _ESP32_BOARDS_ATTR,
    _ESP32_BOARDS_MODULE,
    _esp32_board_pins,
)

pytestmark = pytest.mark.xdist_group("board_sync")


def test_every_esphome_variant_maps_to_enum() -> None:
    module = importlib.import_module(_ESP32_BOARDS_MODULE)
    board_list = getattr(module, _ESP32_BOARDS_ATTR)
    variants = {meta["variant"] for meta in board_list.values()}
    assert variants, "esphome should expose esp32 boards"
    for variant in variants:
        assert Esp32Variant(variant.lower())


def test_catalog_generates_board_with_derived_pins(
    generated_board_catalog: BoardCatalogResponse,
) -> None:
    boards = {b.id: b for b in generated_board_catalog.boards}
    # adafruit_feather_esp32_v2 has no manifest but is in ESP32_BOARD_PINS;
    # it exposes SDA/SCL/TX/RX so derives I2C + UART features on top of the
    # full generic-esp32 matrix.
    assert "adafruit_feather_esp32_v2" in boards
    board = boards["adafruit_feather_esp32_v2"]
    assert board.esphome.platform.value == "esp32"
    assert board.esphome.variant is Esp32Variant.ESP32
    feats = {f for pin in board.pins for f in pin.features}
    assert {PinFeature.I2C_SDA, PinFeature.I2C_SCL, PinFeature.UART_TX} <= feats
    # Aliases enrich, not replace, the variant pinout: the full generic-esp32
    # GPIO set is present, not just the aliased pins.
    assert {p.gpio for p in board.pins} == {p.gpio for p in boards["generic-esp32"].pins}
    # Its TX/RX aliases land on flash pins (GPIO7/8); those stay unavailable
    # and gain no bus feature.
    gpio7 = next(p for p in board.pins if p.gpio == 7)
    assert gpio7.available is False and not gpio7.features


def test_sparse_board_keeps_full_variant_pinout(
    generated_board_catalog: BoardCatalogResponse,
) -> None:
    """A board with an LED-only pin map still exposes the whole variant pinout."""
    boards = {b.id: b for b in generated_board_catalog.boards}
    assert "mhetesp32minikit" in boards
    board = boards["mhetesp32minikit"]
    assert board.esphome.variant is Esp32Variant.ESP32
    generic_gpios = {p.gpio for p in boards["generic-esp32"].pins}
    assert {p.gpio for p in board.pins} == generic_gpios
    assert {1, 3, 16} <= generic_gpios
    gpio2 = next(p for p in board.pins if p.gpio == 2)
    assert gpio2.occupied_by == "Built-in LED"


def test_esp32_board_pins_overlays_aliases_onto_generic() -> None:
    """Aliases add LED occupancy and bus features; flash and absent pins stay put."""
    generic = [
        BoardPin(gpio=1, label="GPIO1", features=[PinFeature.UART_TX]),
        BoardPin(gpio=2, label="GPIO2"),
        BoardPin(gpio=7, label="GPIO7", available=False, occupied_by="SPI Flash"),
        BoardPin(gpio=21, label="GPIO21"),
    ]
    pins = _esp32_board_pins(generic, {"LED": 2, "SDA": 21, "RX": 7, "MOSI": 6})
    by_gpio = {p.gpio: p for p in pins}
    assert set(by_gpio) == {1, 2, 7, 21}  # GPIO6 has no generic pin, so dropped
    assert by_gpio[2].occupied_by == "Built-in LED"
    assert PinFeature.I2C_SDA in by_gpio[21].features
    assert by_gpio[21].notes and "SDA" in by_gpio[21].notes
    assert by_gpio[1].features == [PinFeature.UART_TX]  # untouched generic pin
    assert by_gpio[7].features == [] and by_gpio[7].available is False  # flash pin


def test_esp32_board_pins_falls_back_to_aliases_without_generic() -> None:
    """No generic manifest for the variant derives bare pins so it is not pinless."""
    pins = _esp32_board_pins([], {"LED": 2, "TX": 1})
    assert {p.gpio for p in pins} == {1, 2}


def test_catalog_falls_back_to_generic_variant_pins(
    generated_board_catalog: BoardCatalogResponse,
) -> None:
    boards = {b.id: b for b in generated_board_catalog.boards}
    # adafruit_camera_esp32s3 has no manifest and no ESP32_BOARD_PINS entry,
    # so it inherits the generic-esp32s3 pinout.
    assert "adafruit_camera_esp32s3" in boards
    board = boards["adafruit_camera_esp32s3"]
    assert board.esphome.variant is Esp32Variant.ESP32S3
    generic = boards["generic-esp32s3"]
    assert board.pins, "fallback board should carry the generic variant pins"
    assert [p.to_dict() for p in board.pins] == [p.to_dict() for p in generic.pins]


def test_generated_boards_dedup_on_id(
    generated_board_catalog: BoardCatalogResponse,
) -> None:
    ids = [b.id for b in generated_board_catalog.boards]
    assert len(ids) == len(set(ids)), "every catalog board id must be unique"
