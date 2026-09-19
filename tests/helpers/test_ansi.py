"""The shared ANSI CSI pattern strips both the escape byte and its literal spelling."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.ansi import ANSI_CSI_RE


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("\x1b[32mINFO ok\x1b[0m", "INFO ok", id="escape_byte"),
        pytest.param("\\033[32mINFO ok\\033[0m", "INFO ok", id="literal_spelling"),
        pytest.param("\x1b[?25l[ 17%] Compiling", "[ 17%] Compiling", id="private_mode"),
        pytest.param("plain [SUCCESS] text", "plain [SUCCESS] text", id="untouched"),
    ],
)
def test_ansi_csi_re_strips_both_spellings(line: str, expected: str) -> None:
    assert ANSI_CSI_RE.sub("", line) == expected
