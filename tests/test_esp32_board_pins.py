"""ESP32 board generation from ESPHome's ``BOARDS`` / ``ESP32_BOARD_PINS``."""

from __future__ import annotations

import importlib

import pytest

from esphome_device_builder.models import BoardCatalogResponse, Esp32Variant, PinFeature
from script.sync_boards import _ESP32_BOARDS_ATTR, _ESP32_BOARDS_MODULE

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
    # it exposes SDA/SCL/TX/RX so derives I2C + UART features.
    assert "adafruit_feather_esp32_v2" in boards
    board = boards["adafruit_feather_esp32_v2"]
    assert board.esphome.platform.value == "esp32"
    assert board.esphome.variant is Esp32Variant.ESP32
    feats = {f for pin in board.pins for f in pin.features}
    assert {PinFeature.I2C_SDA, PinFeature.I2C_SCL, PinFeature.UART_TX} <= feats


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
