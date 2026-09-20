"""Tests for the text helpers."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.text import same_text


@pytest.mark.parametrize(
    ("current", "expected", "same"),
    [
        ("a: 1\n", "a: 1\n", True),
        ("a: 1\n", "a: 1", True),
        ("a: 1", "a: 1\n\n", True),
        ("a: 1\n", "a: 2\n", False),
        ("a: 1\n", "a: 1 ", False),
    ],
)
def test_same_text_ignores_only_trailing_newlines(current: str, expected: str, same: bool) -> None:
    assert same_text(current, expected) is same
