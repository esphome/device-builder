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


def test_diff_excerpt_budgets_a_line_that_starts_like_a_header() -> None:
    expected = "".join(f"-- note {i}\n" for i in range(10))
    current = "".join(f"++ note {i}\n" for i in range(10))
    lines = diff_excerpt(expected, current).splitlines()
    assert lines[:2] == ["--- expected", "+++ current"]
    assert sum(line.startswith("---") for line in lines[2:]) == 6
    assert sum(line.startswith("+++") for line in lines[2:]) == 6


def test_diff_excerpt_drops_the_hunk_headers_of_hidden_changes() -> None:
    expected = "".join(f"k{i}: {i}\n" for i in range(40))
    current = "".join(f"k{i}: {'x' if i % 2 else i}\n" for i in range(40))
    lines = diff_excerpt(expected, current).splitlines()
    assert lines[:2] == ["--- expected", "+++ current"]
    assert sum(line.startswith("@@") for line in lines) == 6
    assert all(not line.startswith("@@") for line in lines[-2:])
    assert lines[-1].endswith(" more diff lines")


def test_diff_excerpt_attaches_a_hunk_header_to_the_line_shown_under_it() -> None:
    expected = "".join(f"k{i}\n" for i in range(10))
    added = "".join(f"new{i}\n" for i in range(8))
    current = "k0\nk1\n" + added + "".join(f"k{i}\n" for i in range(2, 8)) + "k9\n"
    lines = diff_excerpt(expected, current).splitlines()
    assert sum(line.startswith("+") and not line.startswith("+++") for line in lines) == 6
    assert lines[-3].startswith("@@")
    assert lines[-2] == "-k8"
    assert lines[-1].endswith(" more diff lines")
