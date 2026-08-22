"""Tests for the PyYAML mark helpers."""

from __future__ import annotations

from esphome_device_builder.helpers.yaml.marks import marked_paths, trim_marks

_DUP_KEY = (
    'Duplicate key "wifi_password"\n'
    '  in "C:\\Users\\prose\\esphome\\secrets.yaml", line 7, column 1\n'
    "NOTE: Previous declaration here:\n"
    '  in "/config/kitchen.yaml", line 5, column 1'
)


def test_marked_paths_reports_every_marked_document() -> None:
    assert marked_paths(_DUP_KEY) == {
        "C:\\Users\\prose\\esphome\\secrets.yaml",
        "/config/kitchen.yaml",
    }
    assert marked_paths("[esphome] generator regression") == set()


def test_trim_marks_keeps_line_and_column_on_one_line() -> None:
    assert trim_marks(_DUP_KEY) == (
        'Duplicate key "wifi_password" in secrets.yaml, line 7, column 1 '
        "NOTE: Previous declaration here: in kitchen.yaml, line 5, column 1"
    )
