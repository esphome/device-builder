"""nRF52 board-pin generation from ESPHome's ``BOARDS_ZEPHYR`` + ``AIN_TO_GPIO``."""

from __future__ import annotations

from esphome_device_builder.models import PinFeature
from script.sync_boards import _derive_nrf52_pins, build_catalog


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


def test_catalog_generates_nrf52_boards() -> None:
    boards = {b.esphome.board: b for b in build_catalog().boards}
    assert "xiao_ble" in boards, "xiao_ble should be auto-generated from ESPHome board data"
    xiao = boards["xiao_ble"]
    assert xiao.esphome.platform.value == "nrf52"
    assert xiao.name == "Seeed XIAO nRF52840"
    assert xiao.pins, "generated board should carry pins"
    adc_pins = [p for p in xiao.pins if PinFeature.ADC in p.features]
    assert adc_pins, "ADC-capable pins should be tagged"


def test_nrf52_does_not_steal_rp2040_itsybitsy() -> None:
    # `adafruit_itsybitsy` is a board id on both rp2040 and nRF52; id dedup must
    # keep the rp2040 entry (added first) and the nRF52 one stays the suffixed id.
    by_id = {b.id: b for b in build_catalog().boards}
    assert by_id["adafruit_itsybitsy"].esphome.platform.value == "rp2040"
    assert by_id["adafruit_itsybitsy_nrf52840"].esphome.platform.value == "nrf52"
