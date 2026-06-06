"""Tests for the live bare-list detection in ``script/sync_components.py``.

ESPHome marks a field ``is_list`` in the schema bundle only when it
flows through ``cv.ensure_list``; a raw ``[item]`` (often inside
``cv.All([item], extra)``) bypasses that path, so the bundle types it
as a scalar. ``_is_list_validator`` recovers the list shape from the
live validator, and the collect/apply pair promotes such fields to
``multi_value`` so the editor renders a list instead of one input that
drops the YAML list on save. ``binary_sensor.on_multi_click``'s
``timing`` and ``esp32_camera``'s ``data_pins`` are the canonical
cases.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import esphome.config_validation as cv  # noqa: E402
import sync_components  # noqa: E402


def test_is_list_validator_detects_bare_list() -> None:
    assert sync_components._is_list_validator([cv.string]) is True


def test_is_list_validator_detects_all_wrapping_a_list() -> None:
    """``cv.All([item], extra)`` — the ``on_multi_click.timing`` shape."""
    assert sync_components._is_list_validator(cv.All([cv.string], cv.Length(min=1))) is True


def test_is_list_validator_rejects_scalar_and_listless_all() -> None:
    assert sync_components._is_list_validator(cv.string) is False
    assert sync_components._is_list_validator(cv.All(cv.string, cv.Length(min=1))) is False


def test_apply_list_fields_marks_matching_path_multi_value() -> None:
    entries = [
        {"key": "data_pins", "type": "string", "multi_value": False},
        {"key": "name", "type": "string", "multi_value": False},
    ]
    sync_components._apply_list_fields(entries, {("data_pins",): True})
    by_key = {e["key"]: e for e in entries}
    assert by_key["data_pins"]["multi_value"] is True
    assert by_key["name"]["multi_value"] is False


def test_live_trigger_list_params_finds_multi_click_timing() -> None:
    """The bare-list ``timing`` param is recovered from the live binary_sensor schema."""
    params = sync_components._live_trigger_list_params("binary_sensor")
    assert ("on_multi_click", "timing") in params


def test_live_trigger_list_params_empty_for_listless_component() -> None:
    assert sync_components._live_trigger_list_params("switch") == frozenset()


def test_collect_list_fields_flags_data_pins() -> None:
    """``esp32_camera.data_pins`` is a bare ``[pin]`` list the bundle types as a scalar."""
    loader = sync_components._get_esphome_loader()
    manifest = loader.get_component("esp32_camera")
    assert ("data_pins",) in sync_components._collect_list_fields(manifest)
