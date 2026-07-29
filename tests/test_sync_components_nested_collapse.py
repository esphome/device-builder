"""Tests for childless-nested avoidance in the catalog conversion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _AUTOMATIONS_BODIES_DIR,
    _OUTPUT_BODIES_DIR,
    _convert_config_vars,
    _convert_field,
)


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    """Build a schema dir carrying one shared base schema for extends tests."""
    (tmp_path / "base.json").write_text(
        json.dumps(
            {
                "base": {
                    "schemas": {
                        "SHARED": {
                            "schema": {
                                "config_vars": {
                                    "inherited_field": {"type": "string", "key": "Optional"},
                                },
                            },
                        },
                    },
                },
            }
        )
    )
    return tmp_path


def test_empty_schema_wrapper_collapses(schema_dir: Path) -> None:
    """An empty ``type: schema`` wrapper falls through to scalar typing."""
    raw = {"key": "Optional", "schema": {}, "type": "schema", "default": "0.001"}
    entry = _convert_field("current_gain", raw, schema_dir)
    assert entry is not None
    assert entry["type"] != "nested"
    assert not entry.get("config_entries")


def test_local_field_reuses_root_extends_ref(schema_dir: Path) -> None:
    """A local field re-extending the root's ref expands; it isn't a cycle."""
    node = {
        "extends": ["base.SHARED"],
        "config_vars": {
            "std_dev": {
                "key": "Optional",
                "type": "schema",
                "schema": {"extends": ["base.SHARED"]},
            },
        },
    }
    entries = {e["key"]: e for e in _convert_config_vars(node, schema_dir)}
    children = {e["key"] for e in entries["std_dev"].get("config_entries") or []}
    assert "inherited_field" in children


def test_inherited_field_keeps_cycle_guard(schema_dir: Path) -> None:
    """A ref re-extended from *inside* its own expansion stays guarded."""
    (schema_dir / "loop.json").write_text(
        json.dumps(
            {
                "loop": {
                    "schemas": {
                        "SELF": {
                            "schema": {
                                "config_vars": {
                                    "child": {
                                        "key": "Optional",
                                        "type": "schema",
                                        "schema": {"extends": ["loop.SELF"]},
                                    },
                                },
                            },
                        },
                    },
                },
            }
        )
    )
    node = {"extends": ["loop.SELF"], "config_vars": {}}
    entries = {e["key"]: e for e in _convert_config_vars(node, schema_dir)}
    grandchildren = entries["child"].get("config_entries") or []
    assert not any(e.get("config_entries") for e in grandchildren)


def test_shipped_catalog_childless_nested_resolved() -> None:
    """The three #2379 shapes ship resolved: float, trigger, populated nested."""
    body = json.loads((_OUTPUT_BODIES_DIR / "sensor.cs5460a.json").read_text(encoding="utf-8"))
    gain = next(e for e in body["config_entries"] if e["key"] == "current_gain")
    assert gain["type"] == "float"
    assert gain["range"] == [-1, 1]

    body = json.loads((_OUTPUT_BODIES_DIR / "sprinkler.json").read_text(encoding="utf-8"))
    repeat = next(e for e in body["config_entries"] if e["key"] == "repeat_number")
    action = next(e for e in repeat["config_entries"] if e["key"] == "set_action")
    assert action["type"] == "trigger"
    valves = next(e for e in body["config_entries"] if e["key"] == "valves")
    duration = next(e for e in valves["config_entries"] if e["key"] == "run_duration_number")
    valve_action = next(e for e in duration["config_entries"] if e["key"] == "set_action")
    assert valve_action["type"] == "trigger"

    body = json.loads((_OUTPUT_BODIES_DIR / "sensor.combination.json").read_text(encoding="utf-8"))
    std_dev = None

    def find(entries):
        nonlocal std_dev
        for e in entries or []:
            if e["key"] == "std_dev":
                std_dev = e
            find(e.get("config_entries"))

    find(body["config_entries"])
    assert std_dev is not None
    assert std_dev["config_entries"]


def test_shipped_catalog_has_no_childless_nested() -> None:
    """No shipped ``nested`` entry is an empty dead-end group."""
    violations = []

    def walk(entries, name):
        for entry in entries or []:
            if entry.get("type") == "nested" and not entry.get("config_entries"):
                violations.append((name, entry.get("key")))
            walk(entry.get("config_entries"), name)

    for body_path in sorted(_OUTPUT_BODIES_DIR.glob("*.json")):
        body = json.loads(body_path.read_text(encoding="utf-8"))
        walk(body.get("config_entries"), body_path.name)
    for body_path in sorted(_AUTOMATIONS_BODIES_DIR.glob("**/*.json")):
        body = json.loads(body_path.read_text(encoding="utf-8"))
        walk(body.get("config_entries"), body_path.name)
    assert not violations


def test_overridden_field_reusing_root_ref_expands(schema_dir: Path) -> None:
    """A locally-overridden inherited key counts as local for cycle scoping."""
    (schema_dir / "wrap.json").write_text(
        json.dumps(
            {
                "wrap": {
                    "schemas": {
                        "BASE": {
                            "schema": {
                                "config_vars": {
                                    "slot": {"key": "Optional", "type": "string"},
                                },
                            },
                        },
                    },
                },
            }
        )
    )
    node = {
        "extends": ["wrap.BASE"],
        "config_vars": {
            "slot": {
                "key": "Optional",
                "type": "schema",
                "schema": {"extends": ["wrap.BASE"]},
            },
        },
    }
    entries = {e["key"]: e for e in _convert_config_vars(node, schema_dir)}
    children = {e["key"] for e in entries["slot"].get("config_entries") or []}
    assert "slot" in children
