"""Unit tests for the automation-extraction pass in ``script/sync_components.py``.

The full integration test (downloading the live schema bundle) is
prohibitively slow and platform-flaky on CI (the cache lives under
``.cache/`` and Windows runners don't share it). Instead, these
tests feed the extractor a hand-crafted in-memory mock schema and
assert the structural decomposition matches what we expect for the
canonical cases: component-scoped action, core action with
``then:`` placeholder, condition with ``accepts_condition_list``,
component trigger with nested params, light effect.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# The sync script lives under ``script/`` and isn't on the package
# path; add it to ``sys.path`` once at module import.
_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import sync_components  # noqa: E402 — sys.path manipulation must precede the import


def _write_schema(tmp_path: Path, filename: str, payload: dict) -> Path:
    """Drop a schema file under tmp_path/schema/ and return the dir."""
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir(exist_ok=True)
    (schema_dir / filename).write_text(json.dumps(payload))
    return schema_dir


def test_build_automations_extracts_component_action(tmp_path: Path) -> None:
    """A component-scoped action emits one entry with the expected shape."""
    schema_dir = _write_schema(
        tmp_path,
        "switch.json",
        {
            "switch": {
                "action": {
                    "toggle": {
                        "maybe": "id",
                        "schema": {
                            "config_vars": {
                                "id": {
                                    "key": "Required",
                                    "type": "use_id",
                                    "use_id_type": "switch_::Switch",
                                },
                            },
                        },
                        "type": "schema",
                        "docs": "Toggle the switch.",
                    },
                },
                "schemas": {},
            },
        },
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    actions = {a["id"]: a for a in result["actions"]}
    assert "switch.toggle" in actions
    toggle = actions["switch.toggle"]
    assert toggle["domain"] == "switch"
    assert toggle["name"] == "Switch → Toggle"
    assert toggle["is_control_flow"] is False
    assert toggle["accepts_action_list"] == []


def test_build_automations_strips_then_from_control_flow_action_params(
    tmp_path: Path,
) -> None:
    """``then:`` / ``else:`` placeholders surface on ``accepts_action_list``."""
    schema_dir = _write_schema(
        tmp_path,
        "esphome.json",
        {
            "core": {
                "action": {
                    "if": {
                        "schema": {
                            "config_vars": {
                                "condition": {
                                    "key": "Required",
                                    "registry": "condition",
                                    "type": "registry",
                                },
                                "then": {
                                    "is_list": True,
                                    "key": "Optional",
                                    "registry": "action",
                                    "type": "registry",
                                },
                                "else": {
                                    "is_list": True,
                                    "key": "Optional",
                                    "registry": "action",
                                    "type": "registry",
                                },
                            },
                        },
                        "type": "schema",
                        "docs": "Conditional execution.",
                    },
                },
                "condition": {},
            },
        },
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    if_action = next(a for a in result["actions"] if a["id"] == "if")
    assert if_action["is_control_flow"] is True
    assert if_action["has_else_branch"] is True
    # Stable ordering: ``then`` before ``else``.
    assert if_action["accepts_action_list"] == ["then", "else"]
    # The placeholder keys are stripped from ``config_entries``.
    cfg_keys = {e["key"] for e in if_action["config_entries"]}
    assert "then" not in cfg_keys
    assert "else" not in cfg_keys
    assert "condition" not in cfg_keys


def test_build_automations_extracts_condition_combinator(tmp_path: Path) -> None:
    """A boolean combinator (``and``) declares ``accepts_condition_list=True``."""
    schema_dir = _write_schema(
        tmp_path,
        "esphome.json",
        {
            "core": {
                "action": {},
                "condition": {
                    "and": {
                        "is_list": True,
                        "registry": "condition",
                        "type": "registry",
                        "docs": "All sub-conditions must be true.",
                    },
                },
            },
        },
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    and_cond = next(c for c in result["conditions"] if c["id"] == "and")
    assert and_cond["accepts_condition_list"] is True
    assert and_cond["domain"] == "core"


def test_build_automations_extracts_component_trigger_with_nested_params(
    tmp_path: Path,
) -> None:
    """A trigger schema with config_vars emits trigger params on the catalog entry."""
    schema_dir = _write_schema(
        tmp_path,
        "binary_sensor.json",
        {
            "binary_sensor": {
                "action": {},
                "condition": {},
                "schemas": {
                    "_BINARY_SENSOR_SCHEMA": {
                        "schema": {
                            "config_vars": {
                                "on_click": {
                                    "key": "Optional",
                                    "schema": {
                                        "config_vars": {
                                            "min_length": {
                                                "key": "Optional",
                                                "default": "50ms",
                                                "schema": {
                                                    "extends": [
                                                        "core.positive_time_period_milliseconds",
                                                    ],
                                                },
                                                "type": "schema",
                                            },
                                            "then": {"type": "trigger"},
                                        },
                                    },
                                    "type": "trigger",
                                },
                            },
                        },
                    },
                },
            },
        },
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    on_click = next(t for t in result["triggers"] if t["id"] == "binary_sensor.on_click")
    assert on_click["applies_to"] == ["binary_sensor"]
    assert on_click["is_device_level"] is False
    cfg_keys = {e["key"] for e in on_click["config_entries"]}
    assert "min_length" in cfg_keys
    assert "then" not in cfg_keys  # placeholder stripped


def test_build_automations_extracts_light_effect(tmp_path: Path) -> None:
    """A light effect entry surfaces under ``light_effects`` with its params."""
    schema_dir = _write_schema(
        tmp_path,
        "light.json",
        {
            "light": {
                "action": {},
                "condition": {},
                "effects": {
                    "flicker": {
                        "schema": {
                            "config_vars": {
                                "alpha": {"default": "0.95", "key": "Optional"},
                                "intensity": {"default": "0.015", "key": "Optional"},
                            },
                        },
                        "type": "schema",
                        "docs": "Candle flicker effect.",
                    },
                },
                "schemas": {},
            },
        },
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    flicker = next(e for e in result["light_effects"] if e["id"] == "flicker")
    assert flicker["name"] == "Light → Flicker"
    cfg_keys = {e["key"] for e in flicker["config_entries"]}
    assert "alpha" in cfg_keys
    assert "intensity" in cfg_keys


def test_build_automations_dedupes_by_id(tmp_path: Path) -> None:
    """Duplicate registry entries across files are deduplicated by id."""
    # Two schema files both register ``switch.toggle`` — should
    # produce a single output entry.
    schema_dir = _write_schema(
        tmp_path,
        "switch.json",
        {
            "switch": {
                "action": {
                    "toggle": {"type": "schema", "docs": "first"},
                },
                "schemas": {},
            },
        },
    )
    (schema_dir / "switch_dup.json").write_text(
        json.dumps(
            {
                "switch": {
                    "action": {
                        "toggle": {"type": "schema", "docs": "dup"},
                    },
                    "schemas": {},
                },
            },
        ),
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    matching = [a for a in result["actions"] if a["id"] == "switch.toggle"]
    assert len(matching) == 1
