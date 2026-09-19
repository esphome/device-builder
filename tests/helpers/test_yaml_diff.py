"""``apply_yaml_diff`` follows the editor's splice rule for both diff shapes."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.yaml import apply_yaml_diff

_TEXT = "a: 1\nb: 2\nc: 3\n"


def test_replace_range() -> None:
    diff = {"fromLine": 2, "toLine": 3, "replacement": "x: 9\n"}
    assert apply_yaml_diff(_TEXT, diff) == "a: 1\nx: 9\n"


def test_pure_insert_before_line() -> None:
    diff = {"fromLine": 2, "toLine": 1, "replacement": "new: 0\n"}
    assert apply_yaml_diff(_TEXT, diff) == "a: 1\nnew: 0\nb: 2\nc: 3\n"


def test_delete_range() -> None:
    diff = {"fromLine": 1, "toLine": 2, "replacement": ""}
    assert apply_yaml_diff(_TEXT, diff) == "c: 3\n"


@pytest.mark.parametrize(("from_line", "to_line"), [(0, 1), (5, 4), (1, 4), (3, 1)])
def test_out_of_range_splice_raises(from_line: int, to_line: int) -> None:
    with pytest.raises(ValueError, match="outside 3 lines"):
        apply_yaml_diff(_TEXT, {"fromLine": from_line, "toLine": to_line, "replacement": ""})


def test_append_after_the_last_line() -> None:
    diff = {"fromLine": 4, "toLine": 3, "replacement": "d: 4\n"}
    assert apply_yaml_diff(_TEXT, diff) == "a: 1\nb: 2\nc: 3\nd: 4\n"


def test_counts_lines_like_the_producer() -> None:
    text = "a: 'x\x0cy'\nb: 2\nc: 3\n"
    diff = {"fromLine": 3, "toLine": 3, "replacement": ""}
    assert apply_yaml_diff(text, diff) == "a: 'x\x0cy'\nc: 3\n"
