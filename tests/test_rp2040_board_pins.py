"""RP2040/RP2350 board-pin derivation from ESPHome's ``RP2040_BOARD_PINS``."""

from __future__ import annotations

from esphome_device_builder.models import PinFeature
from script.sync_boards import _derive_rp2040_pins, build_catalog


def test_derive_enumerates_full_gpio_matrix_with_pwm() -> None:
    pins = _derive_rp2040_pins(
        {"SDA": 4, "SCL": 5, "TX": 0, "RX": 1, "MOSI": 19, "MISO": 16, "SCK": 18, "SS": 17}, 29
    )
    assert [p.gpio for p in pins] == list(range(30))
    assert all(PinFeature.PWM in p.features for p in pins)
    by_gpio = {p.gpio: p for p in pins}
    assert PinFeature.UART_TX in by_gpio[0].features
    assert by_gpio[0].notes == "Default UART0 TX"
    assert PinFeature.I2C_SDA in by_gpio[4].features
    assert PinFeature.SPI_CS in by_gpio[17].features
    assert PinFeature.ADC in by_gpio[26].features and "ADC0" in (by_gpio[26].notes or "")
    assert PinFeature.ADC in by_gpio[29].features and "ADC3" in (by_gpio[29].notes or "")


def test_rp2350b_adc_range_is_40_47() -> None:
    by_gpio = {p.gpio: p for p in _derive_rp2040_pins({}, 47)}
    assert len(by_gpio) == 48
    assert PinFeature.ADC in by_gpio[40].features and "ADC0" in (by_gpio[40].notes or "")
    assert PinFeature.ADC in by_gpio[47].features and "ADC7" in (by_gpio[47].notes or "")
    # 26-29 are NOT ADC on the 48-GPIO rp2350B.
    assert PinFeature.ADC not in by_gpio[26].features


def test_led_becomes_occupied_and_virtual_pin_dropped() -> None:
    by_gpio = {p.gpio: p for p in _derive_rp2040_pins({"LED": 25}, 29)}
    assert by_gpio[25].occupied_by == "Built-in LED"
    assert PinFeature.PWM in by_gpio[25].features
    # The CYW43 virtual LED (gpio 64, past max_pin) is dropped, not emitted.
    pins = _derive_rp2040_pins({"LED": 64}, 29)
    assert all(p.gpio <= 29 for p in pins)


def test_catalog_generates_unmanifested_rp2040_board() -> None:
    boards = {b.id: b for b in build_catalog().boards}
    # 0xcb_helios is in ESPHome's BOARDS but has no device-builder manifest.
    assert "0xcb_helios" in boards
    board = boards["0xcb_helios"]
    assert board.esphome.platform.value == "rp2040"
    assert [p.gpio for p in board.pins] == list(range(30))


def test_dedup_keeps_hand_curated_manifest() -> None:
    boards = {b.id: b for b in build_catalog().boards}
    # rpipico stays the hand-curated entry — its built-in LED note survives.
    assert any(p.occupied_by == "Built-in LED" for p in boards["rpipico"].pins)
