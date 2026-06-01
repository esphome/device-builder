"""Parser tests for ``controllers/automations/parsing.py``.

Walks the fixture YAMLs in ``tests/fixtures/automation_yamls/`` and
pins the structural-decomposition behaviour of the round-trip
parser: device-level vs inline component triggers, both YAML
shortcut forms, top-level script + interval blocks, recursive
``then`` / ``else`` decomposition, the condition gate, lambdas as
the ``{"_lambda": ...}`` sentinel, light effects, and per-automation
error isolation (one unknown id flags its own entry, not the parse).
"""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest

from esphome_device_builder.controllers.automations.parsing import parse_device_yaml
from esphome_device_builder.helpers.api import CommandError

_FIXTURES = Path(__file__).parent / "fixtures" / "automation_yamls"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Device-level triggers
# ---------------------------------------------------------------------------


def test_parse_device_on_boot_with_priority_param() -> None:
    """The on_boot handler surfaces its ``priority`` as a trigger_param."""
    parsed = parse_device_yaml(_load("device_on_boot.yaml"))
    assert len(parsed) == 1
    item = parsed[0]
    assert item.location.kind == "device_on"
    assert item.location.trigger == "on_boot"
    assert item.automation.trigger_id == "on_boot"
    assert item.automation.trigger_params == {"priority": 200}
    actions = item.automation.actions
    assert [a.action_id for a in actions] == ["delay", "light.turn_on"]
    assert actions[0].params == {"id": "1s"}
    assert actions[1].params == {"id": "living_room"}


# ---------------------------------------------------------------------------
# Inline component triggers — both YAML shortcut forms
# ---------------------------------------------------------------------------


def test_parse_inline_on_press_with_explicit_then() -> None:
    """The explicit ``then:`` shape decomposes correctly."""
    parsed = parse_device_yaml(_load("inline_on_press.yaml"))
    assert len(parsed) == 1
    item = parsed[0]
    assert item.location.kind == "component_on"
    assert item.location.component_id == "kitchen_button"
    assert item.location.trigger == "on_press"
    assert [a.action_id for a in item.automation.actions] == [
        "switch.toggle",
        "delay",
    ]


def test_parse_inline_on_press_with_bare_action_list() -> None:
    """The bare-action-list shortcut parses to the same shape.

    ``on_press: - switch.toggle: ...`` (no ``then:`` key) is ESPHome's
    shorthand for ``on_press: { then: [- switch.toggle: ...] }``.
    Both must decompose into the same :class:`AutomationTree`.
    """
    bare = parse_device_yaml(_load("inline_on_press_bare_actionlist.yaml"))
    explicit = parse_device_yaml(_load("inline_on_press.yaml"))
    assert len(bare) == len(explicit) == 1
    bare_tree = bare[0].automation
    explicit_tree = explicit[0].automation
    assert [a.action_id for a in bare_tree.actions] == [a.action_id for a in explicit_tree.actions]
    assert [a.params for a in bare_tree.actions] == [a.params for a in explicit_tree.actions]


def test_parse_inline_on_press_with_single_action_shortcut() -> None:
    """The single-action shortcut ``on_press: switch.toggle: relay1`` parses.

    A trigger body that's a bare mapping of one (or more) known
    catalog action ids becomes an action list with no trigger
    params — the same shape as the explicit-``then:`` form. Pin
    the action surfaces correctly and ``trigger_params`` stays
    empty (an earlier shape erroneously absorbed the action key
    into ``trigger_params``).
    """
    parsed = parse_device_yaml(_load("inline_on_press_single_action.yaml"))
    assert len(parsed) == 1
    tree = parsed[0].automation
    assert tree.trigger_params == {}
    assert [a.action_id for a in tree.actions] == ["switch.toggle"]
    assert tree.actions[0].params == {"id": "relay1"}


def test_parse_inline_on_click_surfaces_trigger_params() -> None:
    """``on_click.min_length`` / ``max_length`` are trigger_params, not actions."""
    parsed = parse_device_yaml(_load("on_click_with_params.yaml"))
    assert len(parsed) == 1
    tree = parsed[0].automation
    assert tree.trigger_id == "binary_sensor.on_click"
    assert tree.trigger_params == {"min_length": "50ms", "max_length": "350ms"}
    assert [a.action_id for a in tree.actions] == ["switch.toggle"]


def test_parse_on_value_range_float_params_are_json_serialisable() -> None:
    """Decimal on_value_range thresholds round-trip as plain floats."""
    parsed = parse_device_yaml(_load("sensor_on_value_range_float.yaml"))
    tree = parsed[0].automation
    assert tree.trigger_id == "sensor.on_value_range"
    assert tree.trigger_params == {"above": 3.14, "below": 25.5}
    assert type(tree.trigger_params["above"]) is float  # not ScalarFloat
    # The real regression: the WS layer must be able to serialise the result.
    orjson.dumps([p.to_dict() for p in parsed])


# ---------------------------------------------------------------------------
# LVGL actions — oversized forms fall back to raw-YAML editing
# ---------------------------------------------------------------------------


def test_parse_oversized_lvgl_action_falls_back_to_raw_yaml() -> None:
    """An oversized LVGL action parses as an unknown id (raw-YAML fallback)."""
    parsed = parse_device_yaml(_load("lvgl_action_unsupported.yaml"))
    assert len(parsed) == 1
    assert parsed[0].error is not None
    assert parsed[0].automation.actions == []


def test_parse_small_lvgl_action_stays_editable() -> None:
    """A small LVGL action (``lvgl.pause``) still decomposes normally."""
    parsed = parse_device_yaml(_load("lvgl_action_small_editable.yaml"))
    assert len(parsed) == 1
    assert parsed[0].error is None
    assert [a.action_id for a in parsed[0].automation.actions] == ["lvgl.pause"]


# ---------------------------------------------------------------------------
# Top-level blocks
# ---------------------------------------------------------------------------


def test_parse_script_with_parameters_and_mode() -> None:
    """A top-level ``script:`` block surfaces its mode + parameters."""
    parsed = parse_device_yaml(_load("script_with_parameters.yaml"))
    assert len(parsed) == 1
    item = parsed[0]
    assert item.location.kind == "script"
    assert item.location.id == "morning_alarm"
    tree = item.automation
    assert tree.trigger_id is None
    # mode, parameters, and (the implicit id) live on trigger_params.
    assert tree.trigger_params["mode"] == "single"
    # ``parameters:`` is a dict the wire shape passes through as-is.
    assert tree.trigger_params["parameters"] == {"hour": "int", "message": "string"}
    assert [a.action_id for a in tree.actions] == ["logger.log", "delay"]


def test_parse_interval_block() -> None:
    """A top-level ``interval:`` list item surfaces an :class:`IntervalLocation`."""
    parsed = parse_device_yaml(_load("interval.yaml"))
    assert len(parsed) == 1
    item = parsed[0]
    assert item.location.kind == "interval"
    assert item.location.index == 0
    assert item.automation.trigger_params["interval"] == "60s"
    assert [a.action_id for a in item.automation.actions] == ["lambda"]
    # Lambda body is surfaced as the {_lambda: source} sentinel under the
    # lambda action's ``lambda`` shorthand key (its config-entry key).
    lambda_body = item.automation.actions[0].params["lambda"]
    assert (
        isinstance(lambda_body, dict)
        and "_lambda" in lambda_body
        and "ESP_LOGI" in lambda_body["_lambda"]
    )


# ---------------------------------------------------------------------------
# api.actions
# ---------------------------------------------------------------------------


def test_parse_api_action_surfaces_action_name_and_then() -> None:
    """A bare ``api.actions:`` item parses to one ``api_action`` entry."""
    parsed = parse_device_yaml(_load("api_action_simple.yaml"))
    assert len(parsed) == 1
    item = parsed[0]
    assert item.location.kind == "api_action"
    assert item.location.action_name == "start_laundry"
    assert item.label == "API: start_laundry"
    tree = item.automation
    assert tree.trigger_id is None
    assert tree.trigger_params == {}
    assert [a.action_id for a in tree.actions] == ["logger.log"]


def test_parse_api_action_surfaces_variables_as_trigger_params() -> None:
    """``variables:`` survives as a dict on ``trigger_params``."""
    parsed = parse_device_yaml(_load("api_action_with_variables.yaml"))
    assert len(parsed) == 1
    tree = parsed[0].automation
    # The discriminator key is implicit via location; only sibling
    # fields (``variables:`` here) surface on trigger_params.
    assert "action" not in tree.trigger_params
    assert tree.trigger_params["variables"] == {"message": "string", "urgency": "int"}


def test_parse_api_action_emits_one_entry_per_item() -> None:
    """Multiple ``api.actions:`` siblings each yield their own ParsedAutomation."""
    parsed = parse_device_yaml(_load("api_actions_multiple.yaml"))
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == [
        "start_laundry",
        "stop_laundry",
    ]


def test_parse_api_action_decomposes_nested_if() -> None:
    """An api-action whose ``then:`` carries an ``if`` decomposes recursively."""
    parsed = parse_device_yaml(_load("api_action_with_if.yaml"))
    assert len(parsed) == 1
    actions = parsed[0].automation.actions
    assert len(actions) == 1
    if_node = actions[0]
    assert if_node.action_id == "if"
    assert set(if_node.children) == {"then", "else"}


def test_parse_api_action_accepts_legacy_service_key() -> None:
    """The deprecated ``service:`` discriminator parses to the same shape."""
    legacy = (
        "esphome:\n  name: x\n"
        "api:\n  actions:\n"
        "    - service: legacy_name\n"
        "      then:\n        - delay: 1s\n"
    )
    parsed = parse_device_yaml(legacy)
    assert len(parsed) == 1
    assert parsed[0].location.kind == "api_action"
    assert parsed[0].location.action_name == "legacy_name"


def test_parse_api_block_without_actions_returns_empty() -> None:
    """An ``api:`` block without an ``actions:`` key yields no api_action entries.

    The api block carries unrelated configuration (encryption, password,
    port, ...) that's not an automation surface.
    """
    yaml = "esphome:\n  name: x\napi:\n  encryption:\n    key: 'aaaa'\n"
    parsed = parse_device_yaml(yaml)
    assert [p for p in parsed if p.location.kind == "api_action"] == []


def test_parse_api_actions_skips_malformed_items() -> None:
    """Items missing the discriminator or with a non-dict shape are silently skipped.

    Defensive against mid-edit YAMLs where the user has typed a
    partial item — surfacing an error would block the parse for
    every other valid entry.
    """
    yaml = (
        "esphome:\n  name: x\n"
        "api:\n  actions:\n"
        "    - then:\n        - delay: 1s\n"  # no action: key
        "    - action: good\n      then:\n        - delay: 2s\n"
    )
    parsed = parse_device_yaml(yaml)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["good"]


def test_parse_api_actions_skips_non_dict_list_items() -> None:
    """A bare scalar item in ``actions:`` is silently skipped, not raised.

    A mid-edit YAML can carry a stray ``- foo`` while the user is
    typing — losing every following valid item to a parse error
    would be hostile.
    """
    yaml = (
        "esphome:\n  name: x\n"
        "api:\n  actions:\n"
        "    - bogus_scalar\n"
        "    - action: real\n      then:\n        - delay: 1s\n"
    )
    parsed = parse_device_yaml(yaml)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["real"]


# ---------------------------------------------------------------------------
# Recursive / control-flow
# ---------------------------------------------------------------------------


def test_parse_if_then_else_recurses_with_condition_gate() -> None:
    """``if`` decomposes into ``children={"then": [...], "else": [...]}`` + conditions."""
    parsed = parse_device_yaml(_load("if_then_else.yaml"))
    assert len(parsed) == 1
    actions = parsed[0].automation.actions
    assert len(actions) == 1
    if_node = actions[0]
    assert if_node.action_id == "if"
    # The condition gate is surfaced on the action's ``conditions``
    # list, not buried in ``params``.
    assert len(if_node.conditions) == 1
    assert if_node.conditions[0].condition_id == "switch.is_on"
    assert if_node.conditions[0].params == {"id": "relay1"}
    # ``children`` carries the recursive ``then`` / ``else`` action
    # lists, keyed by the schema key from ``accepts_action_list``.
    assert set(if_node.children) == {"then", "else"}
    assert [a.action_id for a in if_node.children["then"]] == ["switch.turn_off"]
    assert [a.action_id for a in if_node.children["else"]] == ["switch.turn_on"]


# ---------------------------------------------------------------------------
# Lambdas
# ---------------------------------------------------------------------------


def test_parse_lambda_action_surfaces_lambda_sentinel() -> None:
    """A ``lambda: |-`` block parses to ``{"_lambda": "<source>"}`` on params."""
    parsed = parse_device_yaml(_load("lambda_action.yaml"))
    actions = parsed[0].automation.actions
    assert len(actions) == 1
    assert actions[0].action_id == "lambda"
    # The bare scalar surfaces under the lambda action's ``lambda``
    # shorthand key (its config-entry key); the value carries the sentinel.
    body = actions[0].params["lambda"]
    assert isinstance(body, dict)
    assert "_lambda" in body
    assert "ESP_LOGI" in body["_lambda"]


def test_parse_tagged_lambda_scalars_render_as_sentinel() -> None:
    """!lambda-tagged scalars (plain + ``|`` block) parse to the sentinel and are JSON-safe."""
    parsed = parse_device_yaml(_load("lambda_tagged_scalars.yaml"))
    by_kind = {p.location.kind: p for p in parsed}

    # Script: ``- delay: !lambda return 0;`` → params={"id": {"_lambda": "return 0;"}}.
    script_actions = by_kind["script"].automation.actions
    assert [a.action_id for a in script_actions] == ["delay"]
    assert script_actions[0].params == {"id": {"_lambda": "return 0;"}}

    # Interval: ``- lambda: !lambda |`` block → params={"lambda": {"_lambda": "<body>"}}.
    interval_actions = by_kind["interval"].automation.actions
    assert [a.action_id for a in interval_actions] == ["lambda"]
    interval_body = interval_actions[0].params["lambda"]
    assert isinstance(interval_body, dict)
    assert interval_body.get("_lambda", "").strip().endswith("return;")

    # The whole response round-trips through orjson — TaggedScalar
    # would have raised "Type is not JSON serializable" here.
    orjson.dumps([p.to_dict() for p in parsed])


# ---------------------------------------------------------------------------
# Light effects
# ---------------------------------------------------------------------------


def test_parse_light_effects_emits_one_entry_per_list_item() -> None:
    """Each effect on a light's ``effects:`` list becomes its own ParsedAutomation."""
    parsed = parse_device_yaml(_load("light_effects.yaml"))
    # Only the light effects yield entries; the platform-binary light
    # itself doesn't carry an on_* handler in this fixture.
    effects = [p for p in parsed if p.location.kind == "light_effect"]
    assert len(effects) == 2
    assert effects[0].location.component_id == "my_lamp"
    assert effects[0].location.index == 0
    assert "flicker" in effects[0].automation.trigger_params
    assert "pulse" in effects[1].automation.trigger_params


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_parse_isolates_unknown_action_id() -> None:
    """An unknown action id flags only its own automation; siblings parse (#1050)."""
    # A clearly-unknown action id (not a real trigger that decompose
    # might learn to handle later) pins the isolation behaviour itself.
    yaml = (
        "esphome:\n  name: x\n"
        "switch:\n"
        "  - platform: gpio\n"
        "    id: sw\n"
        "    on_turn_on:\n"
        "      then:\n"
        "        - logger.log:\n"
        "            format: ok\n"
        "    on_turn_off:\n"
        "      then:\n"
        "        - not_a_real_action: 5\n"
    )
    parsed = parse_device_yaml(yaml)
    good = next(p for p in parsed if p.location.trigger == "on_turn_on")
    bad = next(p for p in parsed if p.location.trigger == "on_turn_off")
    # The broken entry carries its error and an empty tree...
    assert bad.error is not None
    assert "not_a_real_action" in bad.error
    assert bad.automation.actions == []
    # ...while the sibling automation parses normally.
    assert good.error is None
    assert [a.action_id for a in good.automation.actions] == ["logger.log"]


def test_parse_raises_on_unloadable_yaml() -> None:
    """A YAML that won't load at all is the one whole-document failure that raises."""
    with pytest.raises(CommandError):
        parse_device_yaml("switch:\n  - name: [unterminated\n")


# ---------------------------------------------------------------------------
# List-shaped triggers (time.on_time)
# ---------------------------------------------------------------------------


def test_parse_on_time_list_emits_one_entry_per_item() -> None:
    """Each entry of a list-form on_time is its own indexed automation."""
    parsed = parse_device_yaml(_load("time_on_time_list.yaml"))
    assert len(parsed) == 2
    first, second = parsed
    assert first.location.kind == "component_on"
    assert first.location.component_id == "my_time"
    assert first.location.trigger == "on_time"
    assert first.location.index == 0
    assert second.location.index == 1
    assert first.automation.trigger_params == {"seconds": 0, "minutes": 30, "hours": 8}
    assert second.automation.trigger_params == {"cron": "0 0 12 * * *"}
    assert [a.action_id for a in first.automation.actions] == ["logger.log"]
    assert first.error is None and second.error is None


def test_parse_on_time_single_mapping_uses_index_none() -> None:
    """The single-mapping on_time form stays one automation with index None."""
    yaml = (
        "time:\n  - platform: sntp\n    id: my_time\n"
        "    on_time:\n      seconds: 0\n      hours: 8\n"
        "      then:\n        - logger.log: hi\n"
    )
    parsed = parse_device_yaml(yaml)
    assert len(parsed) == 1
    assert parsed[0].location.index is None
    assert parsed[0].automation.trigger_params == {"seconds": 0, "hours": 8}


def test_parse_on_time_list_isolates_one_bad_entry() -> None:
    """An unknown action in one on_time entry flags only that entry."""
    yaml = (
        "time:\n  - platform: sntp\n    id: my_time\n"
        "    on_time:\n"
        "      - seconds: 0\n        then:\n          - logger.log: ok\n"
        "      - hours: 8\n        then:\n          - not_a_real_action: 5\n"
    )
    parsed = parse_device_yaml(yaml)
    assert len(parsed) == 2
    assert parsed[0].error is None
    assert parsed[1].error is not None
    assert "not_a_real_action" in parsed[1].error
    assert parsed[1].automation.actions == []


def test_parse_bare_action_list_is_not_list_form() -> None:
    """A bare action list (on_press) is one automation, not per-item entries."""
    parsed = parse_device_yaml(_load("inline_on_press_bare_actionlist.yaml"))
    component_on = [p for p in parsed if p.location.kind == "component_on"]
    assert all(p.location.index is None for p in component_on)


def test_parse_unknown_action_in_list_surfaces_as_error() -> None:
    """A bare action list with an unknown id stays one entry with its error set."""
    yaml = (
        "binary_sensor:\n  - platform: gpio\n    id: btn\n"
        "    on_press:\n      - not_a_real_action: 5\n"
    )
    parsed = parse_device_yaml(yaml)
    assert len(parsed) == 1
    assert parsed[0].location.index is None
    assert parsed[0].error is not None
    assert "not_a_real_action" in parsed[0].error


def test_parse_returns_empty_for_minimal_yaml() -> None:
    """A YAML without any recognised automation shape parses to empty list."""
    assert parse_device_yaml("esphome:\n  name: x\n") == []


# ---------------------------------------------------------------------------
# Line ranges
# ---------------------------------------------------------------------------


def test_parsed_entries_carry_valid_line_ranges() -> None:
    """Every parsed entry's ``from_line`` is positive and ``to_line >= from_line``."""
    parsed = parse_device_yaml(_load("device_on_boot.yaml"))
    for item in parsed:
        assert item.from_line >= 1
        assert item.to_line >= item.from_line
