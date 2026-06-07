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
    # The ``maybe`` shorthand key surfaces as ``scalar_shorthand_key``.
    assert toggle["scalar_shorthand_key"] == "id"


def test_build_automations_captures_value_and_absent_shorthand_keys(tmp_path: Path) -> None:
    """``maybe`` becomes ``scalar_shorthand_key``; its absence yields ``None``."""
    schema_dir = _write_schema(
        tmp_path,
        "logger.json",
        {
            "logger": {
                "action": {
                    "log": {
                        "maybe": "format",
                        "schema": {
                            "config_vars": {"format": {"key": "Required", "type": "string"}}
                        },
                        "type": "schema",
                        "docs": "Log a message.",
                    },
                    "set_level": {
                        "schema": {"config_vars": {"level": {"key": "Required", "type": "enum"}}},
                        "type": "schema",
                        "docs": "Set the log level.",
                    },
                },
                "schemas": {},
            },
        },
    )
    actions = {
        a["id"]: a
        for a in sync_components.build_automations(schema_dir=schema_dir, component_ids=set())[
            "actions"
        ]
    }
    assert actions["logger.log"]["scalar_shorthand_key"] == "format"
    assert actions["logger.set_level"]["scalar_shorthand_key"] is None


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


def test_build_automations_not_accepts_condition_list_without_is_list(tmp_path: Path) -> None:
    """``not`` is list-accepting even though its schema body lacks ``is_list``."""
    schema_dir = _write_schema(
        tmp_path,
        "esphome.json",
        {
            "core": {
                "action": {},
                "condition": {
                    "not": {
                        "registry": "condition",
                        "type": "registry",
                        "docs": "The sub-condition must be false.",
                    },
                },
            },
        },
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    not_cond = next(c for c in result["conditions"] if c["id"] == "not")
    assert not_cond["accepts_condition_list"] is True


def test_build_automations_flips_platform_scoped_action_id(tmp_path: Path) -> None:
    """Platform-scoped action id flips to ESPHome's ``<domain>.<platform>`` wire form."""
    schema_dir = _write_schema(
        tmp_path,
        "template.json",
        {
            "template.sensor": {
                "action": {
                    "publish": {
                        "schema": {
                            "config_vars": {
                                "id": {"key": "Required", "type": "use_id"},
                                "state": {"key": "Required", "templatable": True},
                            },
                        },
                        "type": "schema",
                        "docs": "Publish a state to a template sensor.",
                    },
                },
            },
        },
    )
    result = sync_components.build_automations(
        schema_dir=schema_dir, component_ids={"sensor.template"}
    )
    ids = {a["id"] for a in result["actions"]}
    assert "sensor.template.publish" in ids
    assert "template.sensor.publish" not in ids
    publish = next(a for a in result["actions"] if a["id"] == "sensor.template.publish")
    assert publish["domain"] == "sensor.template"


def test_build_automations_flips_platform_scoped_condition_id(tmp_path: Path) -> None:
    """Platform-scoped condition id flips to the same ``<domain>.<platform>`` wire form."""
    schema_dir = _write_schema(
        tmp_path,
        "duty_time.json",
        {
            "duty_time.sensor": {
                "condition": {
                    "is_running": {
                        "schema": {"config_vars": {"id": {"key": "Required", "type": "use_id"}}},
                        "type": "schema",
                        "docs": "Check whether the duty-time sensor is running.",
                    },
                },
            },
        },
    )
    result = sync_components.build_automations(
        schema_dir=schema_dir, component_ids={"sensor.duty_time"}
    )
    ids = {c["id"] for c in result["conditions"]}
    assert "sensor.duty_time.is_running" in ids
    assert "duty_time.sensor.is_running" not in ids


def test_build_automations_keeps_in_component_namespace_action_id_verbatim(
    tmp_path: Path,
) -> None:
    """A dotted prefix whose base isn't a platform domain stays verbatim (``espnow.peer``)."""
    schema_dir = _write_schema(
        tmp_path,
        "espnow.json",
        {
            "espnow.peer": {
                "action": {
                    "add": {
                        "schema": {"config_vars": {"peer": {"key": "Required"}}},
                        "type": "schema",
                        "docs": "Add an ESP-NOW peer.",
                    },
                },
            },
        },
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    ids = {a["id"] for a in result["actions"]}
    assert "espnow.peer.add" in ids
    assert "peer.espnow.add" not in ids


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
    # Carries per-entry params (min_length) -> repeatable.
    assert on_click["repeatable"] is True
    cfg_keys = {e["key"] for e in on_click["config_entries"]}
    assert "min_length" in cfg_keys
    assert "then" not in cfg_keys  # placeholder stripped


def test_build_automations_trigger_params_are_not_advanced(tmp_path: Path) -> None:
    """A trigger's per-entry params surface on the main form, not behind 'advanced'."""
    schema_dir = _write_schema(
        tmp_path,
        "time.json",
        {
            "time": {
                "schemas": {
                    "TIME_SCHEMA": {
                        "schema": {
                            "config_vars": {
                                "on_time": {
                                    "key": "Optional",
                                    "type": "trigger",
                                    "schema": {
                                        "config_vars": {
                                            # Optional + not an "important" key: the name-based
                                            # heuristic would default it to advanced.
                                            "seconds": {"key": "Optional"},
                                            "then": {"type": "trigger"},
                                        }
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    on_time = next(t for t in result["triggers"] if t["id"] == "time.on_time")
    seconds = next(e for e in on_time["config_entries"] if e["key"] == "seconds")
    assert not seconds.get("advanced")


def test_build_automations_promotes_multi_click_timing_to_multi_value(tmp_path: Path) -> None:
    """Live detection marks the bare-list ``timing`` param ``multi_value``, not a scalar sibling."""
    # The synthetic bundle supplies the param *entries* (the dumper leaves
    # both untyped); the ``multi_value`` signal comes from introspecting
    # the live ``binary_sensor`` module, keyed on the real trigger/param
    # names — so the same shape under a different name would not be promoted.
    schema_dir = _write_schema(
        tmp_path,
        "binary_sensor.json",
        {
            "binary_sensor": {
                "schemas": {
                    "BINARY_SENSOR_SCHEMA": {
                        "schema": {
                            "config_vars": {
                                "on_multi_click": {
                                    "key": "Optional",
                                    "type": "trigger",
                                    "schema": {
                                        "config_vars": {
                                            "timing": {"key": "Required"},
                                            "invalid_cooldown": {"key": "Optional"},
                                            "then": {"type": "trigger"},
                                        }
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    trigger = next(t for t in result["triggers"] if t["id"] == "binary_sensor.on_multi_click")
    entries = {e["key"]: e for e in trigger["config_entries"]}
    assert entries["timing"]["multi_value"] is True
    # ``invalid_cooldown`` is a scalar, not a bare list — detection is selective.
    assert entries["invalid_cooldown"].get("multi_value") is not True


def test_build_automations_skips_non_dict_schema_bodies_and_non_trigger_vars(
    tmp_path: Path,
) -> None:
    """Malformed schema bodies and non-trigger config_vars are skipped, not emitted."""
    schema_dir = _write_schema(
        tmp_path,
        "x.json",
        {
            "x": {
                "schemas": {
                    "BAD": "not a dict",
                    "OK": {"schema": {"config_vars": {"foo": {"key": "Optional"}}}},
                },
            },
        },
    )
    result = sync_components.build_automations(schema_dir=schema_dir, component_ids=set())
    assert all(t["id"] != "x.foo" for t in result["triggers"])


def test_build_automations_derives_repeatable_from_per_entry_params(tmp_path: Path) -> None:
    """Per-entry params mark a component trigger repeatable; paramless and device-level don't."""
    _params = {"config_vars": {"seconds": {"key": "Optional"}, "then": {"type": "trigger"}}}
    _bare = {"config_vars": {"then": {"type": "trigger"}}}
    schema_dir = _write_schema(
        tmp_path,
        "demo.json",
        {
            "demo": {
                "schemas": {
                    "DEMO_SCHEMA": {
                        "schema": {
                            "config_vars": {
                                "on_schedule": {
                                    "key": "Optional",
                                    "schema": _params,
                                    "type": "trigger",
                                },
                                "on_press": {"key": "Optional", "schema": _bare, "type": "trigger"},
                            },
                        },
                    },
                },
            },
        },
    )
    # Device-level section (``esphome``) with a params trigger.
    _write_schema(
        tmp_path,
        "esphome.json",
        {
            "esphome": {
                "schemas": {
                    "ESPHOME_SCHEMA": {
                        "schema": {
                            "config_vars": {
                                "on_boot": {
                                    "key": "Optional",
                                    "schema": _params,
                                    "type": "trigger",
                                },
                            },
                        },
                    },
                },
            },
        },
    )
    triggers = {
        t["id"]: t
        for t in sync_components.build_automations(schema_dir=schema_dir, component_ids=set())[
            "triggers"
        ]
    }
    assert triggers["demo.on_schedule"]["repeatable"] is True
    assert triggers["demo.on_press"]["repeatable"] is False
    # Device-level handlers carry params but grow inline, never stacked by index.
    assert triggers["on_boot"]["is_device_level"] is True
    assert triggers["on_boot"]["repeatable"] is False


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


def test_core_lambda_action_synthesizes_lambda_field() -> None:
    """The core ``lambda`` action gets a single LAMBDA field + shorthand key.

    The schema bundle carries no ``schema`` for the bare lambda block, so the
    extractor would otherwise leave ``config_entries`` empty and the visual
    editor would render no editor (#1119).
    """
    action = sync_components._convert_automation_action(
        top_key="core",
        domain="core",
        wire_prefix="core",
        name="lambda",
        body={"docs": "Run C++."},
        schema_dir=Path("/unused"),
    )
    assert action is not None
    assert action["scalar_shorthand_key"] == "lambda"
    assert action["config_entries"] == [
        {
            "key": "lambda",
            "type": "lambda",
            "label": "Lambda",
            "description": "Run C++.",
            "required": True,
            "help_link": sync_components._CORE_LAMBDA_DOCS,
        }
    ]


def test_core_lambda_condition_synthesizes_lambda_field() -> None:
    """The core ``lambda`` condition gets the same synthesized LAMBDA field."""
    condition = sync_components._convert_automation_condition(
        top_key="core",
        domain="core",
        wire_prefix="core",
        name="lambda",
        body={"docs": "Return a bool."},
        schema_dir=Path("/unused"),
    )
    assert condition is not None
    assert condition["scalar_shorthand_key"] == "lambda"
    assert [e["type"] for e in condition["config_entries"]] == ["lambda"]
    assert condition["config_entries"][0]["key"] == "lambda"
    assert condition["config_entries"][0]["required"] is True
