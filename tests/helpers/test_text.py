"""Tests for the text helpers."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.text import diff_excerpt, same_text


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


def test_diff_excerpt_shows_the_changed_lines() -> None:
    excerpt = diff_excerpt("a: 1\nb: 2\n", "a: 1\nb: 3\n")
    assert excerpt.splitlines() == ["--- expected", "+++ current", "@@ -2 +2 @@", "-b: 2", "+b: 3"]


def test_diff_excerpt_shows_both_sides_of_a_wholesale_rewrite() -> None:
    expected = "".join(f"k{i}: {i}\n" for i in range(30))
    current = "".join(f"k{i}: x\n" for i in range(30))
    lines = diff_excerpt(expected, current).splitlines()
    assert sum(line.startswith("-") and not line.startswith("---") for line in lines) == 6
    assert sum(line.startswith("+") and not line.startswith("+++") for line in lines) == 6
    assert lines[-1].startswith("... ") and lines[-1].endswith(" more diff lines")
