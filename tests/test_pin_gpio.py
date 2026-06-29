"""Pin ``parse_board_gpio`` resolving every platform pin form to a board GPIO."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.pin_gpio import parse_board_gpio


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # esp / esp8266 / rp2040: bare int or GPIOn
        (12, 12),
        ("12", 12),
        ("GPIO12", 12),
        ("gpio5", 5),
        # bk72xx (LibreTiny/Beken) P{n}
        ("P26", 26),
        ("P0", 0),
        # rtl87xx / ln882x port-A PA{n}
        ("PA02", 2),
        ("PA7", 7),
        # ln882x port-B PB{n} = 16 + n
        ("PB03", 19),
        ("PB0", 16),
        # nRF52 P{port}.{pin} = port*32 + pin
        ("P0.27", 27),
        ("P1.1", 33),
        # un-resolvable -> None
        ("P0.33", None),  # nRF52 pin >= 32 rejected, not folded
        ("PA_0", None),
        ("D3", None),
        ("PWM4", None),
        ("!lambda return 1;", None),
        (True, None),  # bool is an int subclass but never a pin
        (None, None),
    ],
)
def test_parse_board_gpio(value: object, expected: int | None) -> None:
    assert parse_board_gpio(value) == expected
