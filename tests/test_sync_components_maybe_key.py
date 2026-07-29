"""Tests for the ``maybe_simple_value`` marker (``maybe_key``) in the catalog conversion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _convert_field,
    _strip_entry_defaults,
)


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    return tmp_path


def _microphone_raw(**extra: object) -> dict:
    return {
        "key": "Optional",
        "type": "schema",
        "maybe": "microphone",
        "schema": {
            "config_vars": {
                "microphone": {
                    "key": "GeneratedID",
                    "type": "use_id",
                    "use_id_type": "microphone::Microphone",
                },
                "gain_factor": {"key": "Optional", "type": "integer", "default": "1"},
            },
        },
        **extra,
    }


def test_maybe_marker_stamps_maybe_key_on_a_nested_list(schema_dir: Path) -> None:
    """The ``voice_assistant.microphone`` shape: scalar-or-list of mappings."""
    entry = _convert_field("microphone", _microphone_raw(is_list=True), schema_dir)
    assert entry is not None
    assert entry["type"] == "nested"
    assert entry["multi_value"] is True
    assert entry["maybe_key"] == "microphone"


def test_maybe_marker_stamps_maybe_key_on_a_single_nested(schema_dir: Path) -> None:
    """The ``micro_wake_word.microphone`` shape: scalar-or-mapping, no list."""
    entry = _convert_field("microphone", _microphone_raw(), schema_dir)
    assert entry is not None
    assert entry["multi_value"] is False
    assert entry["maybe_key"] == "microphone"


def test_maybe_marker_inherited_through_extends_is_stamped(schema_dir: Path) -> None:
    """The ``sensor.msa3xx`` shape: ``maybe`` lives on the extended base schema."""
    (schema_dir / "base.json").write_text(
        json.dumps(
            {
                "base": {
                    "schemas": {
                        "ACCEL": {
                            "maybe": "name",
                            "schema": {
                                "config_vars": {
                                    "name": {"key": "Optional", "type": "string"},
                                },
                            },
                        },
                    },
                },
            }
        )
    )
    raw = {"key": "Optional", "type": "schema", "schema": {"extends": ["base.ACCEL"]}}
    entry = _convert_field("acceleration_x", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "nested"
    assert entry["maybe_key"] == "name"


def test_nested_without_maybe_marker_emits_none(schema_dir: Path) -> None:
    raw = _microphone_raw(is_list=True)
    del raw["maybe"]
    entry = _convert_field("microphone", raw, schema_dir)
    assert entry is not None
    assert entry["maybe_key"] is None


def test_bool_schema_node_emits_none(schema_dir: Path) -> None:
    """Pin-style fields carry ``schema: true``; only a mapping has extends."""
    raw = {"key": "Optional", "type": "pin", "schema": True}
    entry = _convert_field("pin", raw, schema_dir)
    assert entry is not None
    assert entry["maybe_key"] is None


def test_strip_entry_defaults_drops_a_null_maybe_key() -> None:
    assert "maybe_key" not in _strip_entry_defaults({"key": "x", "maybe_key": None})
    assert _strip_entry_defaults({"key": "x", "maybe_key": "microphone"})["maybe_key"] == (
        "microphone"
    )
