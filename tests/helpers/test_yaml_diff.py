"""``apply_yaml_diff`` follows the producers' splice rule for both diff shapes."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.yaml import apply_yaml_diff, splice_lines
from esphome_device_builder.models.automations import YamlDiff
from tests.conftest import apply_yaml_diff_like_frontend

_TEXT = "a: 1\nb: 2\nc: 3\n"


def _diff(from_line: int, to_line: int, replacement: str) -> YamlDiff:
    return YamlDiff(fromLine=from_line, toLine=to_line, replacement=replacement)


def test_replace_range() -> None:
    assert apply_yaml_diff(_TEXT, _diff(2, 3, "x: 9\n")) == "a: 1\nx: 9\n"


def test_pure_insert_before_line() -> None:
    assert apply_yaml_diff(_TEXT, _diff(2, 1, "new: 0\n")) == "a: 1\nnew: 0\nb: 2\nc: 3\n"


def test_delete_range() -> None:
    assert apply_yaml_diff(_TEXT, _diff(1, 2, "")) == "c: 3\n"


@pytest.mark.parametrize(
    ("from_line", "to_line", "problem"),
    [
        (0, 1, "outside 3 lines"),
        (5, 4, "outside 3 lines"),
        (1, 4, "outside 3 lines"),
        (3, 1, "inverted"),
    ],
)
def test_malformed_splice_raises(from_line: int, to_line: int, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        apply_yaml_diff(_TEXT, _diff(from_line, to_line, ""))


def test_append_after_the_last_line() -> None:
    assert apply_yaml_diff(_TEXT, _diff(4, 3, "d: 4\n")) == "a: 1\nb: 2\nc: 3\nd: 4\n"


@pytest.mark.parametrize(("from_line", "to_line"), [(0, 1), (5, 4), (1, 4), (3, 1)])
def test_out_of_range_splice_raises(from_line: int, to_line: int) -> None:
    with pytest.raises(ValueError, match="outside 3 lines"):
        apply_yaml_diff(_TEXT, {"fromLine": from_line, "toLine": to_line, "replacement": ""})


def test_append_after_the_last_line() -> None:
    diff = {"fromLine": 4, "toLine": 3, "replacement": "d: 4\n"}
    assert apply_yaml_diff(_TEXT, diff) == "a: 1\nb: 2\nc: 3\nd: 4\n"


def test_counts_lines_like_the_producer() -> None:
    text = "a: 'x\x0cy'\nb: 2\nc: 3\n"
    assert apply_yaml_diff(text, _diff(3, 3, "")) == "a: 'x\x0cy'\nc: 3\n"


def test_form_feed_inside_a_scalar_is_not_a_boundary_to_repair() -> None:
    text = "a: 'x\x0cy'\nb: 2\n"
    assert apply_yaml_diff(text, _diff(2, 1, "z: 0\n")) == "a: 'x\x0cz: 0\ny'\nb: 2\n"


def test_append_after_an_unterminated_last_line_keeps_the_boundary() -> None:
    assert apply_yaml_diff("a: 1", _diff(2, 1, "b: 2\n")) == "a: 1\nb: 2"


def test_a_splice_reaching_an_unterminated_last_line_leaves_it_unterminated() -> None:
    assert apply_yaml_diff("a: 1\nb: 2", _diff(2, 2, "c: 3\n")) == "a: 1\nc: 3"
    assert apply_yaml_diff("a: 1\nb: 2", _diff(2, 2, "")) == "a: 1"
    assert apply_yaml_diff("a: 1\r\nb: 2", _diff(3, 2, "c: 3\r\n")) == "a: 1\r\nb: 2\r\nc: 3"


@pytest.mark.parametrize("boundary", ["\n", "\r", "\r\n", "\x0c", "\x85", "\u2028"])
def test_append_after_any_splitlines_boundary_adds_nothing(boundary: str) -> None:
    text = f"a: 1{boundary}"
    assert apply_yaml_diff(text, _diff(2, 1, "b: 2\n")) == f"{text}b: 2\n"


def test_deleting_the_only_line_leaves_nothing() -> None:
    assert apply_yaml_diff("a: 1", _diff(1, 1, "")) == ""


def test_splice_lines_returns_the_text_and_the_matching_diff() -> None:
    lines = _TEXT.splitlines(keepends=True)
    new_text, diff = splice_lines(lines, start=1, end=2, replacement="x: 9\n")
    assert new_text == "a: 1\nx: 9\nc: 3\n"
    assert diff == _diff(2, 2, "x: 9\n")
    assert apply_yaml_diff(_TEXT, diff) == new_text


def test_splice_lines_insert_is_the_pure_insert_diff() -> None:
    new_text, diff = splice_lines(["a: 1"], start=1, end=1, replacement="b: 2\n")
    assert new_text == "a: 1\nb: 2"
    assert diff == _diff(2, 1, "b: 2\n")


@pytest.mark.parametrize("replacement", ["", "z: 9\n", "z: 9", "y: 8\nz: 9\n"])
@pytest.mark.parametrize(
    "text", ["", "a: 1", "a: 1\n", "a: 1\nb: 2", "a: 1\n\nb: 2\n", "a: 1\r\nb: 2"]
)
def test_every_splice_matches_the_frontend_splice(text: str, replacement: str) -> None:
    lines = text.splitlines(keepends=True)
    for start in range(len(lines) + 1):
        for end in range(start, len(lines) + 1):
            new_text, diff = splice_lines(lines, start=start, end=end, replacement=replacement)
            frontend = apply_yaml_diff_like_frontend(
                text, diff.fromLine, diff.toLine, diff.replacement
            )
            # The frontend splits on "\n" alone, so it can strand a "\r" the core drops.
            assert new_text.replace("\r", "") == frontend.replace("\r", ""), (start, end)
