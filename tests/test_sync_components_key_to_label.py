"""Tests for ``_key_to_label`` acronym handling in ``script/sync_components.py``.

Plain ``str.title()`` Title-cases acronyms (``Cs Pin``, ``Ble Id``,
``Mac Address``), which renders badly in the visual editor — issue
#401. Pin the acronym + trailing-digit handling here so a regression
to the old behaviour shows up as a failing test rather than as a
diff in every form label after a re-sync.
"""

from __future__ import annotations

import pytest

from script.sync_components import _key_to_label  # type: ignore[import-not-found]


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        # Bare acronyms render upper-case.
        ("id", "ID"),
        ("ssid", "SSID"),
        ("mqtt", "MQTT"),
        ("uuid", "UUID"),
        # Acronym + descriptive token.
        ("cs_pin", "CS Pin"),
        ("rst_pin", "RST Pin"),
        ("pa_pin", "PA Pin"),
        ("led_pin", "LED Pin"),
        ("mosi_pin", "MOSI Pin"),
        ("vsync_pin", "VSYNC Pin"),
        ("rgb_order", "RGB Order"),
        ("mac_address", "MAC Address"),
        # Acronym + acronym (the ``..._id`` shape the issue calls out).
        ("ble_id", "BLE ID"),
        ("i2c_id", "I2C ID"),
        ("spi_id", "SPI ID"),
        ("uart_id", "UART ID"),
        # Trailing-digit acronyms — alpha prefix is upper-cased,
        # digits stay attached.
        ("dio0_pin", "DIO0 Pin"),
        ("co2_value", "CO2 Value"),
        # Non-acronym tokens keep Title-case (regression guard against
        # the acronym set leaking into ordinary words).
        ("name", "Name"),
        ("friendly_name", "Friendly Name"),
        ("update_interval", "Update Interval"),
        ("day_of_week", "Day Of Week"),
        # Trailing digits on a non-acronym alpha prefix stay as-is —
        # part numbers like ``bme680`` shouldn't accidentally get
        # caught by the regex when ``BME`` isn't an acronym.
        ("bme680_id", "Bme680 ID"),
    ],
)
def test_key_to_label_handles_known_acronyms(key: str, expected: str) -> None:
    assert _key_to_label(key) == expected
