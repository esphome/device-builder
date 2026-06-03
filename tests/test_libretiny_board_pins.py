"""LibreTiny board-pin derivation from ESPHome's ``*_BOARD_PINS``."""

from __future__ import annotations

from esphome_device_builder.models import PinFeature
from script.sync_boards import (
    _alias_capability,
    _derive_pins_from_aliases,
    build_catalog,
)


def test_alias_capability_maps_fixed_functions() -> None:
    assert _alias_capability("SERIAL1_TX") == (PinFeature.UART_TX, "UART1 TX")
    assert _alias_capability("TX2") == (PinFeature.UART_TX, "UART2 TX")
    assert _alias_capability("RX") == (PinFeature.UART_RX, "UART RX")
    assert _alias_capability("WIRE2_SCL") == (PinFeature.I2C_SCL, "I2C2 SCL")
    assert _alias_capability("SDA1") == (PinFeature.I2C_SDA, "I2C1 SDA")
    assert _alias_capability("SPI0_MOSI") == (PinFeature.SPI_MOSI, "SPI0 MOSI")
    assert _alias_capability("ADC3") == (PinFeature.ADC, "ADC")
    assert _alias_capability("PWM0") == (PinFeature.PWM, "PWM")


def test_alias_capability_ignores_positional_and_flexible() -> None:
    # Positional pin names carry no capability.
    assert _alias_capability("D4") is None
    assert _alias_capability("P6") is None
    assert _alias_capability("PA07") is None
    # Enumerated flexible-mux variants are NOT a fixed bus pin.
    assert _alias_capability("WIRE0_SCL_5") is None
    assert _alias_capability("WIRE0_SDA_12") is None


def test_derive_unions_features_and_skips_flexible_mux() -> None:
    # GPIO0 is both UART2 TX and I2C2 SCL; the flexible WIRE0_SCL_0 mustn't tag it.
    pins = _derive_pins_from_aliases(
        {"TX2": 0, "SERIAL2_TX": 0, "WIRE2_SCL": 0, "WIRE0_SCL_0": 0, "D4": 0, "A0": 3}
    )
    by_gpio = {p.gpio: p for p in pins}
    assert set(by_gpio[0].features) == {PinFeature.UART_TX, PinFeature.I2C_SCL}
    assert by_gpio[0].notes == "UART2 TX • I2C2 SCL"
    assert by_gpio[3].features == [PinFeature.ADC]


def test_catalog_generates_unmanifested_libretiny_board() -> None:
    boards = {b.esphome.board: b for b in build_catalog().boards}
    # bw15 has no device-builder manifest but is in RTL87XX_BOARD_PINS.
    assert "bw15" in boards, "bw15 should be auto-generated from ESPHome board data"
    bw15 = boards["bw15"]
    assert bw15.esphome.platform.value == "rtl87xx"
    assert bw15.pins, "generated board should carry derived pins"
    feats = {f for pin in bw15.pins for f in pin.features}
    assert PinFeature.UART_TX in feats and PinFeature.I2C_SDA in feats
