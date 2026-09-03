"""User-keyed map bases referenced through extends-only wrappers collapse to ``map``."""

from __future__ import annotations

import json
from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    _convert_field,
    _extends_map_schema,
)

# Mirrors the 2026.9 api bundle: ``key_type`` sits on the referenced body
# (``VARIABLES_SCHEMA``), while the action entry is an extends-only wrapper.
_API_JSON = {
    "api": {
        "schemas": {
            "VARIABLES_SCHEMA": {
                "key": "String",
                "key_type": "string",
                "schema": {"config_vars": {"string": {"templatable": True, "type": "string"}}},
                "type": "schema",
            },
            "WRAPPED_VARIABLES_SCHEMA": {
                "schema": {"extends": ["api.VARIABLES_SCHEMA"]},
                "type": "schema",
            },
            "PLAIN_SCHEMA": {
                "schema": {"config_vars": {"port": {"key": "Optional", "type": "integer"}}},
                "type": "schema",
            },
        },
    },
}


def _schema_dir(tmp_path: Path) -> Path:
    (tmp_path / "api.json").write_text(json.dumps(_API_JSON), encoding="utf-8")
    return tmp_path


def test_extends_wrapper_collapses_to_map(tmp_path: Path) -> None:
    """A wrapper extending a ``key_type`` base becomes a map with a value template."""
    raw = {"key": "Optional", "schema": {"extends": ["api.VARIABLES_SCHEMA"]}, "type": "schema"}
    entry = _convert_field("variables", raw, _schema_dir(tmp_path))
    assert entry is not None
    assert entry["type"] == "map"
    (template,) = entry["config_entries"]
    assert template["key"] == "value"
    assert template["type"] == "string"
    assert template["templatable"] is True


def test_nested_extends_chain_resolves_key_type(tmp_path: Path) -> None:
    """The ``key_type`` base is found through an intermediate extends hop."""
    raw = {
        "key": "Optional",
        "schema": {"extends": ["api.WRAPPED_VARIABLES_SCHEMA"]},
        "type": "schema",
    }
    entry = _convert_field("variables", raw, _schema_dir(tmp_path))
    assert entry is not None
    assert entry["type"] == "map"


def test_non_map_base_stays_nested(tmp_path: Path) -> None:
    """A wrapper extending a plain schema keeps the nested group shape."""
    raw = {"key": "Optional", "schema": {"extends": ["api.PLAIN_SCHEMA"]}, "type": "schema"}
    entry = _convert_field("server", raw, _schema_dir(tmp_path))
    assert entry is not None
    assert entry["type"] == "nested"
    assert {c["key"] for c in entry["config_entries"]} == {"port"}


def test_wrapper_with_own_config_vars_is_not_a_map(tmp_path: Path) -> None:
    """Own config_vars beside the extends mean a real nested group, not a map wrapper."""
    inner = {
        "extends": ["api.VARIABLES_SCHEMA"],
        "config_vars": {"topic": {"key": "Optional", "type": "string"}},
    }
    assert _extends_map_schema(inner, _schema_dir(tmp_path)) is None


def test_direct_key_type_still_collapses(tmp_path: Path) -> None:
    """The original raw-entry ``key_type`` path is unchanged."""
    raw = {
        "key": "Optional",
        "key_type": "string",
        "schema": {"config_vars": {"string": {"templatable": True, "type": "string"}}},
        "type": "schema",
    }
    entry = _convert_field("data", raw, _schema_dir(tmp_path))
    assert entry is not None
    assert entry["type"] == "map"
