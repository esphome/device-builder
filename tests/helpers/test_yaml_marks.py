"""Tests for the PyYAML mark helpers."""

from __future__ import annotations

from esphome_device_builder.helpers.yaml.marks import marked_documents, trim_marks

_DUP_KEY = (
    'Duplicate key "wifi_password"\n'
    '  in "C:\\Users\\prose\\esphome\\secrets.yaml", line 7, column 1\n'
    "NOTE: Previous declaration here:\n"
    '  in "/config/kitchen.yaml", line 5, column 1'
)


def test_marked_documents_reports_basenames_for_both_path_styles() -> None:
    assert marked_documents(_DUP_KEY) == {"secrets.yaml", "kitchen.yaml"}
    assert marked_documents("[esphome] generator regression") == set()


def test_trim_marks_keeps_line_and_column_on_one_line() -> None:
    assert trim_marks(_DUP_KEY) == (
        'Duplicate key "wifi_password" in secrets.yaml, line 7, column 1 '
        "NOTE: Previous declaration here: in kitchen.yaml, line 5, column 1"
    )
