"""``apply_yaml_diff`` follows the producers' splice rule for both diff shapes."""

from __future__ import annotations

import pytest

from esphome_device_builder.helpers.yaml import apply_yaml_diff
from esphome_device_builder.models.automations import YamlDiff

_TEXT = "a: 1\nb: 2\nc: 3\n"


def _diff(from_line: int, to_line: int, replacement: str) -> YamlDiff:
    return YamlDiff(fromLine=from_line, toLine=to_line, replacement=replacement)


def test_replace_range() -> None:
    assert apply_yaml_diff(_TEXT, _diff(2, 3, "x: 9\n")) == "a: 1\nx: 9\n"


def test_pure_insert_before_line() -> None:
    assert apply_yaml_diff(_TEXT, _diff(2, 1, "new: 0\n")) == "a: 1\nnew: 0\nb: 2\nc: 3\n"


def test_delete_range() -> None:
    assert apply_yaml_diff(_TEXT, _diff(1, 2, "")) == "c: 3\n"


@pytest.mark.parametrize(("from_line", "to_line"), [(0, 1), (5, 4), (1, 4), (3, 1)])
def test_out_of_range_splice_raises(from_line: int, to_line: int) -> None:
    with pytest.raises(ValueError, match="outside 3 lines"):
        apply_yaml_diff(_TEXT, _diff(from_line, to_line, ""))


def test_append_after_the_last_line() -> None:
    assert apply_yaml_diff(_TEXT, _diff(4, 3, "d: 4\n")) == "a: 1\nb: 2\nc: 3\nd: 4\n"


def test_counts_lines_like_the_producer() -> None:
    text = "a: 'x\x0cy'\nb: 2\nc: 3\n"
    assert apply_yaml_diff(text, _diff(3, 3, "")) == "a: 'x\x0cy'\nc: 3\n"


def test_form_feed_inside_a_scalar_is_not_a_boundary_to_repair() -> None:
    text = "a: 'x\x0cy'\nb: 2\n"
    assert apply_yaml_diff(text, _diff(2, 1, "z: 0\n")) == "a: 'x\x0cz: 0\ny'\nb: 2\n"


def test_append_after_an_unterminated_last_line_keeps_the_boundary() -> None:
    assert apply_yaml_diff("a: 1", _diff(2, 1, "b: 2\n")) == "a: 1\nb: 2\n"


def test_deleting_the_only_line_leaves_nothing() -> None:
    assert apply_yaml_diff("a: 1", _diff(1, 1, "")) == ""
