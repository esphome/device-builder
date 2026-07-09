"""Lift of ``logger.hardware_uart`` from upstream pages into the manifest."""

from __future__ import annotations

import pytest

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _build_esphome_block,
    _extract_logger_hardware_uart,
)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"logger": {"hardware_uart": "UART0"}}, "UART0"),
        ({"logger": {"hardware_uart": "uart0"}}, "UART0"),
        ({"logger": {"hardware_uart": " usb_serial_jtag "}}, "USB_SERIAL_JTAG"),
        ({"logger": {"hardware_uart": "USB_CDC"}}, "USB_CDC"),
        # UART2 is valid on classic ESP32 / bk72xx / ln882x.
        ({"logger": {"hardware_uart": "UART2"}}, "UART2"),
        # Missing / bare / non-dict logger blocks lift nothing.
        ({}, None),
        ({"logger": None}, None),
        ({"logger": {}}, None),
        ({"logger": {"level": "DEBUG"}}, None),
        # Unknown or templated values are dropped, not passed through.
        ({"logger": {"hardware_uart": "UART9"}}, None),
        ({"logger": {"hardware_uart": "${uart}"}}, None),
        ({"logger": {"hardware_uart": 0}}, None),
    ],
)
def test_extract_logger_hardware_uart(config: dict, expected: str | None) -> None:
    assert _extract_logger_hardware_uart(config) == expected


def test_build_esphome_block_carries_logger_hardware_uart() -> None:
    block = _build_esphome_block("esp32", "esp32-p4-evboard", "esp32p4", "esp-idf", "UART0")
    assert block["logger_hardware_uart"] == "UART0"


def test_build_esphome_block_omits_logger_hardware_uart_when_unset() -> None:
    block = _build_esphome_block("esp32", "esp32-p4-evboard", "esp32p4", "esp-idf", None)
    assert "logger_hardware_uart" not in block
