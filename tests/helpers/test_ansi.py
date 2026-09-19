"""The shared ANSI CSI pattern strips both the escape byte and its literal spelling."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.ansi import ANSI_CSI_RE, plain_lines, redact_concealed


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


@pytest.mark.parametrize("esc", ["\x1b", "\\033"], ids=["escape_byte", "literal_spelling"])
def test_redact_concealed_removes_the_wrapped_value(esc: str) -> None:
    assert redact_concealed(f"  password: {esc}[8mhunter2{esc}[28m") == "  password: <removed>"


def test_plain_lines_redacts_before_stripping() -> None:
    lines = ["\x1b[32mINFO ok\x1b[0m\n", "key: \x1b[8mhunter2\x1b[28m\r", "plain"]
    assert plain_lines(lines) == ["INFO ok", "key: <removed>", "plain"]
