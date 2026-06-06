"""Writer / round-trip tests for ``controllers/automations/writing.py``.

Each test follows the same shape: load a fixture, parse, optionally
mutate the tree, write through ``render_upsert`` / ``render_delete``,
re-parse, assert structural equivalence and (where applicable) that
the YAML's structural skeleton survived the round-trip.

Comments / blank lines / key-order preservation lives at the
ruamel layer; pinning it at the parse-write-parse boundary keeps
the tests robust against ruamel-emitter quirks (which differ between
ruamel.yaml minor versions).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.controllers.automations import writing
from esphome_device_builder.controllers.automations.parsing import (
    ComponentTarget,
    parse_device_yaml,
)
from esphome_device_builder.controllers.automations.writing import (
    _delete_subentity_on,
    _subentity_context,
    _upsert_subentity_on,
    render_delete,
    render_upsert,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models.api import ErrorCode
from esphome_device_builder.models.automations import (
    ActionNode,
    ApiActionLocation,
    AutomationTree,
    ComponentActionFieldLocation,
    ComponentOnLocation,
    DeviceOnLocation,
    IntervalLocation,
    LightEffectLocation,
    ScriptLocation,
    YamlDiff,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "automation_yamls"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _apply_diff(text: str, diff: YamlDiff) -> str:
    """Apply a :class:`YamlDiff` exactly as the frontend ``applyYamlDiff`` does."""
    lines = text.split("\n")
    start = diff.fromLine - 1
    delete = max(0, diff.toLine - diff.fromLine + 1)
    replacement = diff.replacement
    replacement = replacement.removesuffix("\n")
    rep_lines = [] if replacement == "" else replacement.split("\n")
    return "\n".join([*lines[:start], *rep_lines, *lines[start + delete :]])


# ---------------------------------------------------------------------------
# Device-level
# ---------------------------------------------------------------------------


def test_round_trip_device_on_boot_preserves_action_list() -> None:
    """Parse → upsert with same tree → parse yields identical actions list."""
    yaml_text = _load("device_on_boot.yaml")
    parsed_first = parse_device_yaml(yaml_text)[0]
    new_text, diff = render_upsert(
        yaml_text,
        tree=parsed_first.automation,
        location=parsed_first.location,
    )
    parsed_second = parse_device_yaml(new_text)
    assert len(parsed_second) == 1
    assert [a.action_id for a in parsed_second[0].automation.actions] == [
        a.action_id for a in parsed_first.automation.actions
    ]
    assert parsed_second[0].automation.trigger_params == parsed_first.automation.trigger_params
    assert diff.fromLine >= 1


def test_upsert_creates_on_boot_when_absent() -> None:
    """An empty ``esphome:`` block gains an ``on_boot:`` handler."""
    text = "esphome:\n  name: x\n"
    new_text, diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="on_boot",
            actions=[ActionNode(action_id="delay", params={"id": "100ms"})],
        ),
        location=DeviceOnLocation(trigger="on_boot"),
    )
    assert "on_boot:" in new_text
    assert "delay: 100ms" in new_text
    assert diff.replacement.strip().startswith("on_boot")


def test_delete_device_on_boot_drops_the_block() -> None:
    """Deleting the ``on_boot`` handler removes it entirely."""
    text = _load("device_on_boot.yaml")
    new_text, diff = render_delete(
        text,
        location=DeviceOnLocation(trigger="on_boot"),
    )
    assert "on_boot:" not in new_text
    assert diff.replacement == ""


# ---------------------------------------------------------------------------
# Inline component
# ---------------------------------------------------------------------------


def test_round_trip_inline_on_press_preserves_actions() -> None:
    """Parse → upsert → parse on an inline component handler stays stable."""
    text = _load("inline_on_press.yaml")
    parsed_first = parse_device_yaml(text)[0]
    new_text, _diff = render_upsert(
        text,
        tree=parsed_first.automation,
        location=parsed_first.location,
    )
    parsed_second = parse_device_yaml(new_text)
    assert len(parsed_second) == 1
    assert parsed_second[0].location == parsed_first.location
    assert [a.action_id for a in parsed_second[0].automation.actions] == [
        a.action_id for a in parsed_first.automation.actions
    ]


def test_upsert_inline_on_press_leaves_sibling_components_untouched() -> None:
    """Adding ``on_press`` to one component doesn't mutate adjacent siblings."""
    text = (
        "esphome:\n  name: x\n"
        "binary_sensor:\n"
        "  - platform: gpio\n"
        "    id: a\n"
        "    pin: GPIO0\n"
        "  - platform: gpio\n"
        "    id: b\n"
        "    pin: GPIO1\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="binary_sensor.on_press",
            actions=[ActionNode(action_id="switch.toggle", params={"id": "r"})],
        ),
        location=ComponentOnLocation(component_id="a", trigger="on_press"),
    )
    # Sibling ``id: b`` survived intact.
    assert "id: b" in new_text
    assert "pin: GPIO1" in new_text
    # The on_press is attached to ``a``, not ``b``.
    a_idx = new_text.index("id: a")
    b_idx = new_text.index("id: b")
    on_press_idx = new_text.index("on_press:")
    assert a_idx < on_press_idx < b_idx, "on_press landed under the wrong instance"


_IDLESS_BINARY_SENSOR = (
    "esphome:\n  name: x\n"
    "binary_sensor:\n"
    "  - platform: gpio\n"
    "    pin: GPIO0\n"
    "    on_press:\n"
    "      - logger.log: pressed\n"
)


def test_round_trip_inline_handler_on_idless_instance() -> None:
    """An inline ``on_*`` on an id-less component (synthetic id) round-trips."""
    parsed = parse_device_yaml(_IDLESS_BINARY_SENSOR)
    assert len(parsed) == 1
    location = parsed[0].location
    assert isinstance(location, ComponentOnLocation)
    assert location.component_id == "binary_sensor_0"
    new_text, _diff = render_upsert(
        _IDLESS_BINARY_SENSOR, tree=parsed[0].automation, location=location
    )
    reparsed = parse_device_yaml(new_text)
    assert len(reparsed) == 1
    assert reparsed[0].location == location
    assert [a.action_id for a in reparsed[0].automation.actions] == ["logger.log"]


def test_delete_inline_handler_on_idless_instance() -> None:
    """Deleting an inline handler on an id-less component removes it."""
    location = parse_device_yaml(_IDLESS_BINARY_SENSOR)[0].location
    new_text, diff = render_delete(_IDLESS_BINARY_SENSOR, location=location)
    assert "on_press:" not in new_text
    assert parse_device_yaml(new_text) == []
    assert diff.replacement == ""


def test_idless_multidomain_trigger_resolves_configured_domain() -> None:
    """``on_turn_on`` spans switch/fan/light; an id-less switch resolves to switch."""
    text = (
        "esphome:\n  name: x\n"
        "switch:\n"
        "  - platform: gpio\n"
        "    pin: GPIO2\n"
        "    on_turn_on:\n"
        "      - logger.log: state\n"
    )
    parsed = parse_device_yaml(text)
    assert len(parsed) == 1
    location = parsed[0].location
    assert location.component_id == "switch_0"
    new_text, _diff = render_upsert(text, tree=parsed[0].automation, location=location)
    assert "switch:" in new_text
    reparsed = parse_device_yaml(new_text)
    assert len(reparsed) == 1
    assert reparsed[0].location == location


# ---------------------------------------------------------------------------
# Flat singleton components (sun:, mqtt:)
# ---------------------------------------------------------------------------


def test_round_trip_inline_sun_preserves_actions_and_siblings() -> None:
    """An id-less ``sun:`` handler round-trips; sibling config keys survive."""
    text = _load("inline_sun_on_sunrise.yaml")
    parsed_first = parse_device_yaml(text)[0]
    new_text, _diff = render_upsert(
        text, tree=parsed_first.automation, location=parsed_first.location
    )
    assert "latitude: 51.0" in new_text
    assert "longitude: -0.1" in new_text
    reparsed = parse_device_yaml(new_text)
    assert len(reparsed) == 1
    assert reparsed[0].location == parsed_first.location


def test_upsert_adds_second_handler_to_sun_block() -> None:
    """A new ``on_sunset`` splices in beside the existing ``on_sunrise``."""
    text = _load("inline_sun_on_sunrise.yaml")
    new_text, diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="sun.on_sunset",
            actions=[ActionNode(action_id="logger.log", params={"format": "sunset"})],
        ),
        location=ComponentOnLocation(component_id="sun", trigger="on_sunset"),
    )
    assert "on_sunrise:" in new_text
    assert "on_sunset:" in new_text
    assert "latitude: 51.0" in new_text
    # Pure-insert flags the empty replaced range.
    assert diff.toLine == diff.fromLine - 1
    assert {p.location.trigger for p in parse_device_yaml(new_text)} == {"on_sunrise", "on_sunset"}


def test_upsert_replaces_existing_handler_on_sun_block() -> None:
    """Re-upserting ``on_sunrise`` replaces it rather than duplicating."""
    text = _load("inline_sun_on_sunrise.yaml")
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="sun.on_sunrise",
            actions=[ActionNode(action_id="logger.log", params={"format": "dawn"})],
        ),
        location=ComponentOnLocation(component_id="sun", trigger="on_sunrise"),
    )
    assert new_text.count("on_sunrise:") == 1
    assert "dawn" in new_text


def test_delete_inline_handler_on_sun_block() -> None:
    """Deleting ``on_sunrise`` drops it but keeps the block's config keys."""
    text = _load("inline_sun_on_sunrise.yaml")
    location = parse_device_yaml(text)[0].location
    new_text, diff = render_delete(text, location=location)
    assert "on_sunrise:" not in new_text
    assert "latitude: 51.0" in new_text
    assert parse_device_yaml(new_text) == []
    assert diff.replacement == ""


def test_delete_one_handler_keeps_sibling_on_singleton() -> None:
    """Deleting one handler on a multi-handler singleton leaves the sibling intact."""
    text = _load("inline_sun_idd.yaml")
    location = ComponentOnLocation(component_id="home_sun", trigger="on_sunrise")
    new_text, diff = render_delete(text, location=location)
    assert "on_sunrise:" not in new_text
    assert "on_sunset:" in new_text
    assert "id: home_sun" in new_text
    assert "latitude: 51.0" in new_text
    assert diff.replacement == ""
    remaining = parse_device_yaml(new_text)
    assert [(p.location.component_id, p.location.trigger) for p in remaining] == [
        ("home_sun", "on_sunset"),
    ]


def test_round_trip_mqtt_singleton_resolves_mqtt_domain() -> None:
    """``mqtt.on_message``/``on_connect`` are ambiguous keys but resolve to ``mqtt:``."""
    text = _load("inline_mqtt_singleton.yaml")
    on_connect = next(p for p in parse_device_yaml(text) if p.location.trigger == "on_connect")
    new_text, _diff = render_upsert(text, tree=on_connect.automation, location=on_connect.location)
    # Reattached under mqtt:, not mis-routed to ble_client/wifi/logger.
    assert "broker: 192.168.1.10" in new_text
    reparsed = {p.location.trigger: p.location.component_id for p in parse_device_yaml(new_text)}
    assert reparsed == {"on_message": "mqtt", "on_connect": "mqtt"}


def test_upsert_inline_handler_on_block_without_handlers() -> None:
    """An ``on_*`` inserts under a singleton block that has only config keys."""
    text = _load("inline_sun_empty.yaml")
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="sun.on_sunrise",
            actions=[ActionNode(action_id="logger.log", params={"format": "sunrise"})],
        ),
        location=ComponentOnLocation(component_id="home_sun", trigger="on_sunrise"),
    )
    assert "on_sunrise:" in new_text
    assert "id: home_sun" in new_text
    reparsed = parse_device_yaml(new_text)
    assert len(reparsed) == 1
    assert reparsed[0].location.component_id == "home_sun"


def test_upsert_inline_handler_on_bodyless_singleton_block() -> None:
    """An ``on_*`` inserts under an id-less singleton with an empty body."""
    text = "esphome:\n  name: x\nsun:\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="sun.on_sunrise",
            actions=[ActionNode(action_id="logger.log", params={"format": "sunrise"})],
        ),
        location=ComponentOnLocation(component_id="sun", trigger="on_sunrise"),
    )
    assert "on_sunrise:" in new_text
    assert parse_device_yaml(new_text)[0].location.component_id == "sun"


def test_upsert_flat_singleton_unknown_id_raises() -> None:
    """A location whose id matches no singleton raises a clean error."""
    text = _load("inline_sun_on_sunrise.yaml")
    with pytest.raises(CommandError) as err:
        render_upsert(
            text,
            tree=AutomationTree(
                trigger_id="sun.on_sunrise",
                actions=[ActionNode(action_id="logger.log", params={"format": "x"})],
            ),
            location=ComponentOnLocation(component_id="nonexistent", trigger="on_sunrise"),
        )
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_stale_positional_id_on_idd_instance_is_refused() -> None:
    """A ``<domain>_<idx>`` id pointing at an instance with a real id is refused."""
    text = "esphome:\n  name: x\nbinary_sensor:\n  - platform: gpio\n    id: real\n    pin: GPIO0\n"
    with pytest.raises(CommandError):
        render_upsert(
            text,
            tree=AutomationTree(
                trigger_id="binary_sensor.on_press",
                actions=[ActionNode(action_id="logger.log", params={"format": "hi"})],
            ),
            location=ComponentOnLocation(component_id="binary_sensor_0", trigger="on_press"),
        )


def test_upsert_valid_automation_leaves_unparseable_sibling_verbatim() -> None:
    """Editing a valid automation never rewrites a broken sibling block (#1050)."""
    text = (
        "esphome:\n  name: x\n"
        "time:\n"
        "  - platform: sntp\n"
        "    id: time_bm8563_1\n"
        "    on_time:\n"
        "      - hours: 2,3,4,5\n"
        "        then:\n"
        "          - logger.log:\n"
        "              format: Test 2\n"
        "switch:\n"
        "  - platform: gpio\n"
        "    id: switch_gpio_1\n"
        "    on_turn_on:\n"
        "      then:\n"
        "        - logger.log:\n"
        "            format: Test 3\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="switch.on_turn_on",
            actions=[ActionNode(action_id="logger.log", params={"format": "Edited"})],
        ),
        location=ComponentOnLocation(component_id="switch_gpio_1", trigger="on_turn_on"),
    )
    # The broken on_time block (which the parser can't decompose) is
    # spliced around, not through — its lines survive byte-for-byte.
    assert "    on_time:\n      - hours: 2,3,4,5\n" in new_text
    assert "          - logger.log:\n              format: Test 2\n" in new_text
    # The edit landed on the switch automation; a lone ``format`` collapses to
    # the ``logger.log`` scalar shorthand (its ``maybe_simple_value`` key).
    assert "- logger.log: Edited" in new_text


def test_round_trip_on_click_preserves_trigger_params() -> None:
    """``on_click.min_length`` / ``max_length`` survive parse → write → parse."""
    text = _load("on_click_with_params.yaml")
    parsed_first = parse_device_yaml(text)[0]
    new_text, _diff = render_upsert(
        text,
        tree=parsed_first.automation,
        location=parsed_first.location,
    )
    parsed_second = parse_device_yaml(new_text)[0]
    assert parsed_second.automation.trigger_params == parsed_first.automation.trigger_params


# ---------------------------------------------------------------------------
# Top-level blocks
# ---------------------------------------------------------------------------


def test_round_trip_script_with_parameters() -> None:
    """A script with ``parameters:`` survives parse → write → parse."""
    text = _load("script_with_parameters.yaml")
    parsed_first = parse_device_yaml(text)[0]
    new_text, _diff = render_upsert(
        text,
        tree=parsed_first.automation,
        location=parsed_first.location,
    )
    parsed_second = parse_device_yaml(new_text)[0]
    assert parsed_second.automation.trigger_params["parameters"] == {
        "hour": "int",
        "message": "string",
    }
    assert parsed_second.automation.trigger_params.get("mode") == "single"


def test_append_diff_into_block_followed_by_others_does_not_duplicate_tail() -> None:
    """A mid-file append's diff reproduces new_text without duplicating trailing blocks."""
    text = (
        "esphome:\n  name: x\n"
        "script:\n  - id: first\n    mode: single\n    then: []\n"
        "ota:\n  - platform: esphome\n"
        "web_server:\n  port: 80\n"
    )
    new_text, diff = render_upsert(
        text,
        tree=AutomationTree(trigger_id=None, trigger_params={"mode": "single"}, actions=[]),
        location=ScriptLocation(id="second"),
    )
    # The diff the frontend applies must reproduce the backend's own
    # post-splice text — the contract the editor relies on.
    assert _apply_diff(text, diff) == new_text
    assert new_text.count("ota:") == 1
    assert new_text.count("web_server:") == 1
    assert {s.location.id for s in parse_device_yaml(new_text)} == {"first", "second"}


def test_round_trip_interval_lambda() -> None:
    """An interval with a lambda body round-trips the sentinel intact."""
    text = _load("interval.yaml")
    parsed_first = parse_device_yaml(text)[0]
    new_text, _diff = render_upsert(
        text,
        tree=parsed_first.automation,
        location=parsed_first.location,
    )
    parsed_second = parse_device_yaml(new_text)[0]
    body_first = parsed_first.automation.actions[0].params
    body_second = parsed_second.automation.actions[0].params
    # The lambda survives as a dict under the lambda action's ``lambda``
    # shorthand key (its config-entry key).
    src_first = body_first["lambda"]["_lambda"]
    src_second = body_second["lambda"]["_lambda"]
    assert "ESP_LOGI" in src_first
    assert src_first.strip() == src_second.strip()


# ---------------------------------------------------------------------------
# Nested control-flow
# ---------------------------------------------------------------------------


def test_round_trip_if_then_else_preserves_recursive_actions() -> None:
    """``if`` round-trips through parse → write → parse with both branches."""
    text = _load("if_then_else.yaml")
    parsed_first = parse_device_yaml(text)[0]
    new_text, _diff = render_upsert(
        text,
        tree=parsed_first.automation,
        location=parsed_first.location,
    )
    parsed_second = parse_device_yaml(new_text)[0]
    if_first = parsed_first.automation.actions[0]
    if_second = parsed_second.automation.actions[0]
    assert if_second.action_id == "if" == if_first.action_id
    assert set(if_second.children) == set(if_first.children) == {"then", "else"}
    assert [a.action_id for a in if_second.children["then"]] == [
        a.action_id for a in if_first.children["then"]
    ]
    assert [a.action_id for a in if_second.children["else"]] == [
        a.action_id for a in if_first.children["else"]
    ]
    assert [c.condition_id for c in if_second.conditions] == [
        c.condition_id for c in if_first.conditions
    ]


# ---------------------------------------------------------------------------
# Light effects
# ---------------------------------------------------------------------------


def test_delete_light_effect_removes_one_list_item() -> None:
    """Deleting effects[0] leaves effects[1] in place."""
    text = _load("light_effects.yaml")
    new_text, _diff = render_delete(
        text,
        location=LightEffectLocation(component_id="my_lamp", index=0),
    )
    # ``flicker`` is gone, ``pulse`` remains.
    assert "flicker" not in new_text
    assert "pulse" in new_text


# ---------------------------------------------------------------------------
# Component action-list config fields (``open_action:`` etc.)
# ---------------------------------------------------------------------------


def test_round_trip_component_action_field_preserves_actions() -> None:
    """Parse → upsert with the same tree → parse keeps the action field stable."""
    text = _load("cover_feedback_actions.yaml")
    first = next(
        p
        for p in parse_device_yaml(text)
        if p.location.kind == "component_action" and p.location.field == "open_action"
    )
    new_text, _diff = render_upsert(text, tree=first.automation, location=first.location)
    second = next(
        p
        for p in parse_device_yaml(new_text)
        if p.location.kind == "component_action" and p.location.field == "open_action"
    )
    assert second.location == first.location
    assert [a.action_id for a in second.automation.actions] == [
        a.action_id for a in first.automation.actions
    ]
    # Bare action list — no ``then:`` wrapper introduced.
    assert "then:" not in new_text


def test_upsert_component_action_field_leaves_siblings_untouched() -> None:
    """Rewriting open_action doesn't disturb close_action / stop_action."""
    text = _load("cover_feedback_actions.yaml")
    loc = ComponentActionFieldLocation(component_id="driveway_gate", field="open_action")
    tree = AutomationTree(
        trigger_id=None,
        actions=[ActionNode(action_id="switch.turn_on", params={"id": "gate_open"})],
    )
    new_text, _diff = render_upsert(text, tree=tree, location=loc)
    assert "close_action:" in new_text
    assert "stop_action:" in new_text
    assert new_text.count("open_action:") == 1


def test_upsert_component_action_field_creates_field_when_absent() -> None:
    """A platform instance with no such field yet gets it spliced in."""
    text = "cover:\n  - platform: feedback\n    id: g\n    device_class: gate\n"
    loc = ComponentActionFieldLocation(component_id="g", field="open_action")
    tree = AutomationTree(
        trigger_id=None,
        actions=[ActionNode(action_id="switch.turn_on", params={"id": "relay"})],
    )
    new_text, _diff = render_upsert(text, tree=tree, location=loc)
    reparsed = next(p for p in parse_device_yaml(new_text) if p.location.kind == "component_action")
    assert reparsed.location == loc
    assert [a.action_id for a in reparsed.automation.actions] == ["switch.turn_on"]


def test_delete_component_action_field_drops_only_that_field() -> None:
    """Deleting close_action keeps open_action / stop_action."""
    text = _load("cover_feedback_actions.yaml")
    loc = ComponentActionFieldLocation(component_id="driveway_gate", field="close_action")
    new_text, diff = render_delete(text, location=loc)
    assert "close_action:" not in new_text
    assert "open_action:" in new_text
    assert "stop_action:" in new_text
    assert diff.replacement == ""


def test_upsert_component_action_field_unknown_id_raises() -> None:
    """An id that matches no component instance is a NOT_FOUND error."""
    loc = ComponentActionFieldLocation(component_id="nope", field="open_action")
    tree = AutomationTree(trigger_id=None, actions=[])
    with pytest.raises(CommandError) as exc:
        render_upsert(_load("cover_feedback_actions.yaml"), tree=tree, location=loc)
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_delete_component_action_field_unknown_id_raises() -> None:
    """Deleting an action field on a non-existent instance is NOT_FOUND."""
    loc = ComponentActionFieldLocation(component_id="nope", field="open_action")
    with pytest.raises(CommandError) as exc:
        render_delete(_load("cover_feedback_actions.yaml"), location=loc)
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_round_trip_component_action_field_on_singleton_hub() -> None:
    """A hub (``opentherm:``, no ``platform:``/``id:``) action field round-trips.

    The id-less singleton resolves on ``component_id == domain`` through
    both the parser and the writer's ``_locate_singleton_instance`` path.
    """
    text = "opentherm:\n  in_pin: 4\n  before_send:\n    - logger.log: sending\n"
    first = next(p for p in parse_device_yaml(text) if p.location.kind == "component_action")
    assert first.location.component_id == "opentherm"
    new_text, _diff = render_upsert(text, tree=first.automation, location=first.location)
    second = next(p for p in parse_device_yaml(new_text) if p.location.kind == "component_action")
    assert second.location == first.location
    assert "then:" not in new_text
    # Deleting it drops the field but keeps the rest of the hub config.
    deleted, _d = render_delete(text, location=first.location)
    assert "before_send" not in deleted
    assert "in_pin: 4" in deleted


def test_upsert_component_action_field_splice_miss_raises(monkeypatch) -> None:
    """Domain resolves but the splice can't locate the instance → NOT_FOUND.

    Force a domain that doesn't host the instance (``my_gate`` is under
    ``cover``, not ``switch``); ``upsert_inline_handler`` then returns
    None and the guard raises.
    """
    monkeypatch.setattr(writing, "resolve_component_domain", lambda *_a, **_k: "switch")
    loc = ComponentActionFieldLocation(component_id="my_gate", field="open_action")
    tree = AutomationTree(
        trigger_id=None,
        actions=[ActionNode(action_id="switch.turn_on", params={"id": "r"})],
    )
    with pytest.raises(CommandError) as exc:
        render_upsert(_load("cover_feedback_actions.yaml"), tree=tree, location=loc)
    assert exc.value.code == ErrorCode.NOT_FOUND


def test_delete_component_action_field_splice_miss_raises(monkeypatch) -> None:
    """Domain resolves but the splice can't locate the instance → NOT_FOUND."""
    monkeypatch.setattr(writing, "resolve_component_domain", lambda *_a, **_k: "switch")
    loc = ComponentActionFieldLocation(component_id="my_gate", field="open_action")
    with pytest.raises(CommandError) as exc:
        render_delete(_load("cover_feedback_actions.yaml"), location=loc)
    assert exc.value.code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# api.actions
# ---------------------------------------------------------------------------


def test_round_trip_api_action_preserves_action_name_and_variables() -> None:
    """Parse → upsert with same tree → parse keeps the api-action stable."""
    text = _load("api_action_with_variables.yaml")
    parsed_first = parse_device_yaml(text)[0]
    new_text, _diff = render_upsert(
        text,
        tree=parsed_first.automation,
        location=parsed_first.location,
    )
    parsed_second = parse_device_yaml(new_text)
    api_entries = [p for p in parsed_second if p.location.kind == "api_action"]
    assert len(api_entries) == 1
    assert api_entries[0].location.action_name == "notify_user"
    assert api_entries[0].automation.trigger_params["variables"] == {
        "message": "string",
        "urgency": "int",
    }


def test_upsert_api_action_creates_block_when_absent() -> None:
    """A YAML with no ``api:`` block gains one when an api-action lands."""
    text = "esphome:\n  name: x\n"
    new_text, diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=ApiActionLocation(action_name="my_action"),
    )
    assert "api:" in new_text
    assert "actions:" in new_text
    assert "- action: my_action" in new_text
    assert "delay: 1s" in new_text
    assert diff.replacement.strip() != ""


def test_upsert_api_action_creates_actions_under_existing_api_block() -> None:
    """An ``api:`` block without an ``actions:`` key gains the key + first item."""
    text = "esphome:\n  name: x\napi:\n  encryption:\n    key: 'aaaa'\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=ApiActionLocation(action_name="my_action"),
    )
    # The new actions key sits under the existing api block, and the
    # encryption key it inherited is untouched.
    assert "encryption:" in new_text
    assert "key: 'aaaa'" in new_text
    assert "actions:" in new_text
    assert "- action: my_action" in new_text


def test_upsert_api_action_appends_to_existing_list() -> None:
    """Appending a new api-action leaves existing siblings byte-stable."""
    text = _load("api_actions_multiple.yaml")
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "5s"})],
        ),
        location=ApiActionLocation(action_name="pause_laundry"),
    )
    parsed = parse_device_yaml(new_text)
    api_names = [p.location.action_name for p in parsed if p.location.kind == "api_action"]
    assert api_names == ["start_laundry", "stop_laundry", "pause_laundry"]
    # Sibling text is still present verbatim.
    assert "Starting laundry cycle" in new_text
    assert "Stopping laundry cycle" in new_text


def test_upsert_api_action_replaces_matching_action_name() -> None:
    """Upserting against an existing ``action_name`` replaces that item in place."""
    text = _load("api_actions_multiple.yaml")
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=ApiActionLocation(action_name="stop_laundry"),
    )
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["start_laundry", "stop_laundry"]
    # The replaced item carries the new ``delay`` action, not the
    # original logger.log.
    stop = next(e for e in api_entries if e.location.action_name == "stop_laundry")
    assert [a.action_id for a in stop.automation.actions] == ["delay"]
    # The unrelated sibling stayed intact.
    assert "Starting laundry cycle" in new_text


def test_delete_api_action_drops_only_matching_item() -> None:
    """Deleting one api-action leaves the other survivors untouched."""
    text = _load("api_actions_multiple.yaml")
    new_text, diff = render_delete(
        text,
        location=ApiActionLocation(action_name="start_laundry"),
    )
    parsed = parse_device_yaml(new_text)
    api_names = [p.location.action_name for p in parsed if p.location.kind == "api_action"]
    assert api_names == ["stop_laundry"]
    assert "Starting laundry cycle" not in new_text
    assert "Stopping laundry cycle" in new_text
    assert diff.replacement == ""


def test_delete_last_api_action_drops_the_actions_key() -> None:
    """Deleting the final api-action leaves no ``actions: []`` noise."""
    text = _load("api_action_simple.yaml")
    new_text, _diff = render_delete(
        text,
        location=ApiActionLocation(action_name="start_laundry"),
    )
    assert "actions:" not in new_text
    assert "start_laundry" not in new_text
    # The ``api:`` block itself is preserved — encryption / password
    # siblings (if present) wouldn't be touched. We don't have one
    # here, so it's just the bare ``api:`` header.
    assert "api:" in new_text


def test_delete_api_action_raises_not_found_when_block_absent() -> None:
    """Deleting from a YAML with no ``api:`` block raises NOT_FOUND."""
    text = "esphome:\n  name: x\n"
    with pytest.raises(CommandError) as err:
        render_delete(text, location=ApiActionLocation(action_name="absent"))
    assert err.value.code == ErrorCode.NOT_FOUND


def test_delete_api_action_raises_not_found_when_actions_key_missing() -> None:
    """An ``api:`` block without an ``actions:`` key is also NOT_FOUND."""
    text = "esphome:\n  name: x\napi:\n  encryption:\n    key: 'aaaa'\n"
    with pytest.raises(CommandError) as err:
        render_delete(text, location=ApiActionLocation(action_name="absent"))
    assert err.value.code == ErrorCode.NOT_FOUND


def test_upsert_api_action_refuses_inline_actions_list() -> None:
    """``actions: []`` (or any inline value) is rejected with INVALID_ARGS.

    A line-based splice can't safely insert dash items below
    ``actions: []`` without producing invalid YAML or doubling the
    key. Surface a clear error so the user converts to a block list
    explicitly.
    """
    text = "esphome:\n  name: x\napi:\n  actions: []\n"
    with pytest.raises(CommandError) as err:
        render_upsert(
            text,
            tree=AutomationTree(
                trigger_id=None,
                actions=[ActionNode(action_id="delay", params={"id": "1s"})],
            ),
            location=ApiActionLocation(action_name="new"),
        )
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_delete_api_action_refuses_inline_actions_list() -> None:
    """Same INVALID_ARGS guard fires on the delete path."""
    text = "esphome:\n  name: x\napi:\n  actions: null\n"
    with pytest.raises(CommandError) as err:
        render_delete(text, location=ApiActionLocation(action_name="anything"))
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_upsert_api_action_appends_into_empty_actions_with_sibling_key() -> None:
    """``actions:`` with no items but a sibling key below gains its first item.

    Hits the ``item_indent`` fallback in ``locate_actions_list`` —
    there's no existing dash to derive the indent from, so the
    canonical child + 2 nesting is used.
    """
    text = "esphome:\n  name: x\napi:\n  actions:\n  encryption:\n    key: 'aaaa'\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=ApiActionLocation(action_name="first"),
    )
    # The new item landed under actions:, encryption: stayed intact.
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["first"]
    assert "encryption:" in new_text
    assert "key: 'aaaa'" in new_text


def test_upsert_api_action_skips_malformed_sibling_during_lookup() -> None:
    """A malformed sibling item (no ``action:`` key) doesn't derail the lookup.

    Hits the ``_discriminator`` returns-None branch — ``find_item``
    walks every list item, and items missing a discriminator are
    silently skipped instead of crashing the upsert.
    """
    text = (
        "esphome:\n  name: x\n"
        "api:\n  actions:\n"
        "    - variables:\n        foo: int\n"  # no action: key at all
        "    - action: real\n      then:\n        - delay: 1s\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "9s"})],
        ),
        location=ApiActionLocation(action_name="real"),
    )
    # The ``real`` action was replaced (not appended as a duplicate).
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["real"]
    assert "delay: 9s" in new_text


def test_upsert_api_action_preserves_blank_lines_in_lambda_block_scalar() -> None:
    """A ``|``-style lambda body with an embedded blank line round-trips intact.

    Padding the blank line with spaces would turn it into a
    whitespace-only line — which YAML treats differently inside a
    literal block scalar than a fully empty line, corrupting the
    lambda's content. Pin that the writer keeps blank lines blank.
    """
    text = "esphome:\n  name: x\n"
    body = 'ESP_LOGI("tag", "before");\n\nESP_LOGI("tag", "after");'
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[
                ActionNode(action_id="lambda", params={"lambda": {"_lambda": body}}),
            ],
        ),
        location=ApiActionLocation(action_name="logme"),
    )
    # The embedded blank line stays a fully-empty line (no leading
    # whitespace).
    assert "\n\n" in new_text
    # Round-trip parses back to the same lambda body.
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert len(api_entries) == 1
    params = api_entries[0].automation.actions[0].params
    src = params["lambda"]["_lambda"]
    assert "before" in src
    assert "after" in src
    assert "\n\n" in src


def test_upsert_api_action_tolerates_actions_key_with_trailing_comment() -> None:
    """``actions: # a note`` is still a block-style key; splice as normal."""
    text = (
        "esphome:\n  name: x\n"
        "api:\n  actions:  # the user added a note here\n"
        "    - action: existing\n      then:\n        - delay: 1s\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "2s"})],
        ),
        location=ApiActionLocation(action_name="newer"),
    )
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["existing", "newer"]
    # Comment survived intact.
    assert "the user added a note here" in new_text


def test_delete_api_action_raises_not_found_when_name_missing() -> None:
    """Deleting an unknown ``action_name`` raises NOT_FOUND."""
    text = _load("api_actions_multiple.yaml")
    with pytest.raises(CommandError) as err:
        render_delete(text, location=ApiActionLocation(action_name="never_added"))
    assert err.value.code == ErrorCode.NOT_FOUND


def test_upsert_api_action_preserves_api_siblings_after_actions() -> None:
    """An ``api:`` block with siblings (encryption, port, ...) keeps them intact.

    Pins the actions-list locator's boundary scan — it has to stop
    at ``encryption:`` (sibling at child indent) rather than
    swallowing the rest of the api block. Without that the splice
    point lands inside the wrong key.
    """
    text = (
        "esphome:\n  name: x\n"
        "api:\n"
        "  actions:\n"
        "    - action: existing\n      then:\n        - delay: 1s\n"
        "\n"
        "  encryption:\n    key: 'aaaa'\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "2s"})],
        ),
        location=ApiActionLocation(action_name="new_one"),
    )
    # Sibling encryption: survived.
    assert "encryption:" in new_text
    assert "key: 'aaaa'" in new_text
    # Both api actions are present, encryption is still its own block.
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["existing", "new_one"]


def test_upsert_api_action_creates_block_when_yaml_has_no_trailing_newline() -> None:
    """A YAML missing its trailing newline still gets a well-formed new api block."""
    text = "esphome:\n  name: x"  # no trailing newline
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=ApiActionLocation(action_name="my_action"),
    )
    assert new_text.endswith("\n")
    assert "- action: my_action" in new_text


def test_upsert_api_action_inserts_actions_key_when_api_has_trailing_blanks() -> None:
    """Trailing blank lines inside the ``api:`` block don't shift the insert point.

    Pins the insert-point trim — the new ``actions:`` key has to land
    above any trailing blank lines so subsequent top-level blocks
    don't collide with it.
    """
    text = "esphome:\n  name: x\napi:\n  encryption:\n    key: 'aaaa'\n\n\nwifi:\n  ssid: x\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=ApiActionLocation(action_name="my_action"),
    )
    # The api block's trailing structure should survive; the new
    # actions key lands above the blank-line gap and wifi remains
    # its own top-level block.
    assert "wifi:" in new_text
    assert "  actions:" in new_text
    # Parser sees both the api action and otherwise valid YAML.
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["my_action"]


def test_upsert_api_action_drops_action_key_smuggled_in_trigger_params() -> None:
    """An explicit ``action`` key on the tree's trigger_params is ignored.

    The discriminator lives on the location, not the tree. A
    hand-built tree may still carry ``action: <name>`` in
    trigger_params (e.g. round-tripped from a pre-rename shape);
    the emitter must not write two ``action:`` lines per item.
    """
    text = "esphome:\n  name: x\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            trigger_params={"action": "ignored_name", "service": "also_ignored"},
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=ApiActionLocation(action_name="real_name"),
    )
    # Only the location-derived action_name is emitted.
    assert "- action: real_name" in new_text
    assert "ignored_name" not in new_text
    assert "also_ignored" not in new_text


def test_upsert_api_action_inserts_under_empty_api_with_section_banner_below() -> None:
    """An empty ``api:`` block followed by a column-0 banner gets canonical indent."""
    text = "esphome:\n  name: x\napi:\n\n# BLE proxy\nesp32_ble_tracker:\n  setup_priority: -500\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(trigger_id=None, actions=[]),
        location=ApiActionLocation(action_name="test"),
    )
    assert "  actions:\n" in new_text
    assert "    - action: test\n" in new_text
    api_idx = new_text.index("api:")
    actions_idx = new_text.index("  actions:")
    banner_idx = new_text.index("# BLE proxy")
    assert api_idx < actions_idx < banner_idx
    assert "# BLE proxy\nesp32_ble_tracker:" in new_text
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["test"]


def test_upsert_api_action_appends_when_column_zero_comment_precedes_actions() -> None:
    """A column-0 comment between ``api:`` and an indented ``actions:`` child is a no-op."""
    text = (
        "esphome:\n  name: x\n"
        "api:\n"
        "# note\n"
        "  actions:\n"
        "    - action: existing\n      then:\n        - delay: 1s\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(trigger_id=None, actions=[]),
        location=ApiActionLocation(action_name="new_one"),
    )
    assert new_text.count("actions:") == 1
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["existing", "new_one"]


def test_upsert_api_action_handles_blank_then_section_banner_after_empty_api() -> None:
    """A blank line between the empty ``api:`` block and a column-0 banner is skipped."""
    text = "esphome:\n  name: x\napi:\n# banner\n\nwifi:\n  ssid: x\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(trigger_id=None, actions=[]),
        location=ApiActionLocation(action_name="test"),
    )
    assert "  actions:\n" in new_text
    assert "    - action: test\n" in new_text
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["test"]


def test_upsert_api_action_handles_trailing_column_zero_comment_at_eof() -> None:
    """A column-0 comment as the last non-blank line stays inside the ``api:`` span."""
    text = "esphome:\n  name: x\napi:\n# trailing\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(trigger_id=None, actions=[]),
        location=ApiActionLocation(action_name="test"),
    )
    assert "  actions:\n" in new_text
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["test"]


def test_upsert_api_action_appends_when_actions_has_trailing_blank() -> None:
    """A trailing blank line below the last item doesn't push the new item past it."""
    text = (
        "esphome:\n  name: x\n"
        "api:\n  actions:\n"
        "    - action: existing\n      then:\n        - delay: 1s\n"
        "\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "2s"})],
        ),
        location=ApiActionLocation(action_name="new_one"),
    )
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    assert [e.location.action_name for e in api_entries] == ["existing", "new_one"]


def test_upsert_api_action_matches_when_discriminator_is_on_a_later_line() -> None:
    """``action:`` doesn't have to be on the dash line — pick it up from a child line.

    Round-trip from the dashboard always emits ``- action: <name>``
    inline, but a hand-edited YAML may carry ``variables:`` or other
    keys above ``action:``. The lookup must find the match either way.
    """
    text = (
        "esphome:\n  name: x\n"
        "api:\n  actions:\n"
        "    - variables:\n        msg: string\n"
        "      action: notify\n"
        "      then:\n        - delay: 1s\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "5s"})],
        ),
        location=ApiActionLocation(action_name="notify"),
    )
    parsed = parse_device_yaml(new_text)
    api_entries = [p for p in parsed if p.location.kind == "api_action"]
    # The replace path took effect (one entry, replaced delay value)
    # rather than an append leaving two ``notify`` siblings.
    assert [e.location.action_name for e in api_entries] == ["notify"]
    assert [a.action_id for a in api_entries[0].automation.actions] == ["delay"]


# ---------------------------------------------------------------------------
# Comment / blank-line preservation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Replace-path / not-found error coverage
# ---------------------------------------------------------------------------


def test_upsert_device_on_creates_esphome_block_when_absent() -> None:
    """A device with no ``esphome:`` block yet gains one when on_boot lands."""
    text = "wifi:\n  ssid: x\n"
    new_text, diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="on_boot",
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=DeviceOnLocation(trigger="on_boot"),
    )
    assert "esphome:" in new_text
    assert "on_boot:" in new_text
    assert diff.fromLine >= 1


def test_upsert_script_with_same_id_replaces_existing_item() -> None:
    """Upserting a script whose id matches an existing entry replaces it in place."""
    text = "esphome:\n  name: x\nscript:\n  - id: alarm\n    then:\n      - delay: 1s\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="logger.log", params={"id": "wake"})],
        ),
        location=ScriptLocation(id="alarm"),
    )
    assert new_text.count("- id: alarm") == 1
    assert "logger.log" in new_text
    assert "delay: 1s" not in new_text


def test_upsert_interval_at_existing_index_replaces_in_place() -> None:
    """An indexed interval upsert at a populated index replaces the item."""
    text = "esphome:\n  name: x\ninterval:\n  - interval: 60s\n    then:\n      - delay: 1s\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id=None,
            trigger_params={"interval": "30s"},
            actions=[ActionNode(action_id="delay", params={"id": "5s"})],
        ),
        location=IntervalLocation(index=0),
    )
    assert new_text.count("- interval:") == 1
    assert "interval: 30s" in new_text


def test_delete_script_not_present_raises_not_found() -> None:
    """Deleting a script with a missing id raises NOT_FOUND."""
    text = "esphome:\n  name: x\n"
    with pytest.raises(CommandError) as err:
        render_delete(text, location=ScriptLocation(id="absent"))
    assert err.value.code == ErrorCode.NOT_FOUND


def test_delete_device_on_when_block_absent_raises_not_found() -> None:
    """Deleting on_boot from a YAML without ``esphome:`` raises NOT_FOUND."""
    text = "wifi:\n  ssid: x\n"
    with pytest.raises(CommandError) as err:
        render_delete(text, location=DeviceOnLocation(trigger="on_boot"))
    assert err.value.code == ErrorCode.NOT_FOUND


def test_delete_light_effect_out_of_range_raises_not_found() -> None:
    """Deleting effects[99] from a one-effect light raises NOT_FOUND."""
    text = _load("light_effects.yaml")
    with pytest.raises(CommandError) as err:
        render_delete(
            text,
            location=LightEffectLocation(component_id="my_lamp", index=99),
        )
    assert err.value.code == ErrorCode.NOT_FOUND


def test_delete_last_light_effect_removes_the_effects_block_entirely() -> None:
    """Deleting the only effect drops ``effects:`` from the light instance."""
    text = (
        "esphome:\n  name: x\n"
        "light:\n  - platform: binary\n    name: lamp\n    id: lamp\n"
        "    output: out\n    effects:\n      - flicker:\n          alpha: 0.9\n"
    )
    new_text, _diff = render_delete(
        text,
        location=LightEffectLocation(component_id="lamp", index=0),
    )
    assert "effects:" not in new_text
    assert "flicker" not in new_text


def test_upsert_component_on_unknown_trigger_raises() -> None:
    """A trigger key that doesn't exist on any domain raises INVALID_ARGS."""
    text = "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
    with pytest.raises(CommandError) as err:
        render_upsert(
            text,
            tree=AutomationTree(trigger_id="bogus.never_exists", actions=[]),
            location=ComponentOnLocation(component_id="btn", trigger="never_exists"),
        )
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_upsert_component_on_diff_carries_inserted_text() -> None:
    """Pure-insert under a component returns the rendered text as ``replacement``.

    The frontend applies the diff client-side; an empty replacement
    on a pure insert (``toLine == fromLine - 1``) means the new
    automation never lands in the user's draft YAML. Reproducer:
    add a Light → On State on a light instance — the YAML pane
    stays unchanged. Pin the contract here so the writer never
    drops the rendered content again.
    """
    text = (
        "light:\n"
        "  - platform: binary\n"
        "    name: Status\n"
        "    id: status_led\n"
        "    output: status_led_out\n"
    )
    _new_text, diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="light.on_state",
            actions=[ActionNode(action_id="light.toggle", params={"id": "status_led"})],
        ),
        location=ComponentOnLocation(component_id="status_led", trigger="on_state"),
    )
    assert diff.replacement.strip() != ""
    assert "on_state:" in diff.replacement
    assert "light.toggle" in diff.replacement


def test_upsert_component_on_resolves_domain_from_yaml_when_trigger_key_is_ambiguous() -> None:
    """The trigger key alone can match multiple domains; use the YAML's actual layout.

    ``on_turn_on`` exists on switch, fan, light, cover, … . With
    only ``component_id="relay"`` + ``trigger="on_turn_on"`` in the
    location, the catalog-only fallback picks the alphabetically
    first domain (``fan``) and the writer fails with
    "instance id='relay' not found under 'fan'". The fix walks the
    YAML to find which top-level block actually configures
    ``id: relay`` and uses that domain.
    """
    text = "switch:\n  - platform: gpio\n    id: relay\n    pin: GPIO5\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="switch.on_turn_on",
            actions=[
                ActionNode(action_id="delay", params={"seconds": "1"}),
            ],
        ),
        location=ComponentOnLocation(component_id="relay", trigger="on_turn_on"),
    )
    assert "on_turn_on:" in new_text
    # The handler must land under the existing switch block — not
    # a fabricated ``fan:`` block.
    assert "fan:" not in new_text


def test_upsert_component_on_ignores_action_reference_to_target_id() -> None:
    """An earlier handler that *references* the id must not steal the domain.

    A shared trigger key (``on_turn_on``) plus a decoy ``id: relay``
    reference nested in an earlier component's action body used to make
    the writer attribute ``relay`` to the decoy's domain (``light``)
    and fail with "instance id='relay' not found under 'light'". Only
    the declared ``switch`` instance owns the id.
    """
    text = (
        "light:\n"
        "  - platform: binary\n"
        "    id: lamp\n"
        "    output: out\n"
        "    on_turn_on:\n"
        "      then:\n"
        "        - switch.turn_off:\n"
        "            id: relay\n"
        "switch:\n"
        "  - platform: gpio\n"
        "    id: relay\n"
        "    pin: GPIO5\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="switch.on_turn_on",
            actions=[ActionNode(action_id="delay", params={"seconds": "1"})],
        ),
        location=ComponentOnLocation(component_id="relay", trigger="on_turn_on"),
    )
    # The new handler landed under the switch instance, beside its
    # existing pin — the light block's decoy reference is untouched.
    switch_block = new_text.split("switch:", 1)[1]
    assert "on_turn_on:" in switch_block
    assert "pin: GPIO5" in switch_block


def test_delete_component_on_missing_instance_raises_not_found() -> None:
    """Deleting on_press on an instance id that doesn't exist raises NOT_FOUND."""
    text = "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
    with pytest.raises(CommandError) as err:
        render_delete(
            text,
            location=ComponentOnLocation(component_id="ghost", trigger="on_press"),
        )
    assert err.value.code == ErrorCode.NOT_FOUND


def test_round_trip_preserves_comments_above_top_level_blocks() -> None:
    """A comment above ``script:`` survives the round-trip on an inline handler.

    The writer touches the inline ``on_press:`` handler only; the
    top-level ``script:`` block (and its leading comment) must
    remain intact bit-for-bit. This is the load-bearing reason for
    using ruamel round-trip mode in the first place.
    """
    text = (
        "esphome:\n  name: x\n\n"
        "# This is a wake-up script\n"
        "script:\n  - id: alarm\n    then:\n      - delay: 1s\n\n"
        "binary_sensor:\n"
        "  - platform: gpio\n"
        "    id: btn\n"
        "    pin: GPIO0\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="binary_sensor.on_press",
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=ComponentOnLocation(component_id="btn", trigger="on_press"),
    )
    # Comment line stayed put.
    assert "# This is a wake-up script\n" in new_text
    # Original script body is untouched.
    assert "- id: alarm" in new_text


# ---------------------------------------------------------------------------
# List-shaped triggers (time.on_time)
# ---------------------------------------------------------------------------


def _on_time_loc(index: int) -> ComponentOnLocation:
    return ComponentOnLocation(component_id="my_time", trigger="on_time", index=index)


def test_round_trip_on_time_list_entry_is_stable() -> None:
    """Parse → upsert an entry with its own tree → re-parse keeps both entries."""
    yaml_text = _load("time_on_time_list.yaml")
    first = parse_device_yaml(yaml_text)[0]
    new_text, _diff = render_upsert(yaml_text, tree=first.automation, location=_on_time_loc(0))
    reparsed = parse_device_yaml(new_text)
    assert [p.location.index for p in reparsed] == [0, 1]
    assert reparsed[0].automation.trigger_params == {"seconds": 0, "minutes": 30, "hours": 8}


def test_upsert_appends_entry_for_non_on_time_repeatable_trigger() -> None:
    """A second entry on a non-on_time repeatable trigger appends, not overwrites."""
    yaml_text = (
        "sensor:\n"
        "  - platform: template\n"
        "    id: s1\n"
        "    name: s\n"
        "    on_value_range:\n"
        "      - above: 10\n"
        "        then:\n"
        "          - logger.log: hi\n"
    )
    tree = AutomationTree(
        trigger_id="sensor.on_value_range",
        trigger_params={"below": 5},
        actions=[ActionNode(action_id="logger.log", params={"format": "lo"})],
    )
    location = ComponentOnLocation(component_id="s1", trigger="on_value_range", index=1)
    new_text, _diff = render_upsert(yaml_text, tree=tree, location=location)
    reparsed = parse_device_yaml(new_text)
    assert [p.location.index for p in reparsed] == [0, 1]
    assert reparsed[1].automation.trigger_params == {"below": 5}


_ON_TIME_COMMENTED = (
    "time:\n  - platform: sntp\n    id: my_time\n    on_time:\n"
    "      # morning schedule\n"
    "      - seconds: 0\n        then:\n          - logger.log: morning\n"
    "\n# unrelated trailing comment\nsensor:\n  - platform: template\n    id: s1\n    name: s\n"
)


def test_upsert_preserves_inner_comments_and_does_not_duplicate_the_trailing_one() -> None:
    """Append keeps an entry's own comment and leaves the after-block comment in place, once."""
    tree = AutomationTree(
        trigger_id="time.on_time",
        trigger_params={"hours": 23},
        actions=[ActionNode(action_id="logger.log", params={"format": "night"})],
    )
    new_text, _diff = render_upsert(
        _ON_TIME_COMMENTED,
        tree=tree,
        location=ComponentOnLocation(component_id="my_time", trigger="on_time", index=1),
    )
    # Inner comment survives; the after-block comment isn't duplicated and stays
    # attached to the following block.
    assert new_text.count("# morning schedule") == 1
    assert new_text.count("# unrelated trailing comment") == 1
    assert "\n# unrelated trailing comment\nsensor:" in new_text
    assert [p.location.index for p in parse_device_yaml(new_text)] == [0, 1]


def test_upsert_drops_a_comment_trailing_the_last_entry() -> None:
    """A comment ruamel binds to the last entry is dropped, never duplicated (accepted trade)."""
    yaml_text = (
        "time:\n  - platform: sntp\n    id: my_time\n    on_time:\n"
        "      - seconds: 0\n        then:\n          - logger.log: hi\n"
        "      # tail of the last entry\n"
        "sensor:\n  - platform: template\n    id: s1\n    name: s\n"
    )
    tree = AutomationTree(
        trigger_id="time.on_time",
        trigger_params={"hours": 23},
        actions=[ActionNode(action_id="logger.log", params={"format": "night"})],
    )
    new_text, _diff = render_upsert(
        yaml_text,
        tree=tree,
        location=ComponentOnLocation(component_id="my_time", trigger="on_time", index=1),
    )
    # Indistinguishable from the after-block comment, so intentionally dropped, not duplicated.
    assert new_text.count("# tail of the last entry") == 0
    assert [p.location.index for p in parse_device_yaml(new_text)] == [0, 1]


def test_delete_entry_does_not_duplicate_the_trailing_comment() -> None:
    """Deleting an entry leaves the after-block comment intact and un-duplicated."""
    yaml_text = (
        "time:\n  - platform: sntp\n    id: my_time\n    on_time:\n"
        "      - seconds: 0\n        then:\n          - logger.log: a\n"
        "      - seconds: 30\n        then:\n          - logger.log: b\n"
        "\n# unrelated trailing comment\nsensor:\n  - platform: template\n    id: s1\n    name: s\n"
    )
    new_text, _diff = render_delete(
        yaml_text, location=ComponentOnLocation(component_id="my_time", trigger="on_time", index=0)
    )
    assert new_text.count("# unrelated trailing comment") == 1
    assert [p.location.index for p in parse_device_yaml(new_text)] == [0]


def test_upsert_on_time_appends_entry_at_end() -> None:
    """An index equal to the entry count appends a new schedule."""
    yaml_text = _load("time_on_time_list.yaml")
    tree = AutomationTree(
        trigger_id="time.on_time",
        trigger_params={"hours": 23},
        actions=[ActionNode(action_id="logger.log", params={"id": "night"})],
    )
    new_text, _diff = render_upsert(yaml_text, tree=tree, location=_on_time_loc(2))
    reparsed = parse_device_yaml(new_text)
    assert [p.location.index for p in reparsed] == [0, 1, 2]
    assert reparsed[2].automation.trigger_params == {"hours": 23}


def test_upsert_on_time_replaces_entry_and_leaves_siblings() -> None:
    """Replacing one entry leaves the other untouched."""
    yaml_text = _load("time_on_time_list.yaml")
    tree = AutomationTree(
        trigger_id="time.on_time",
        trigger_params={"seconds": 5},
        actions=[ActionNode(action_id="logger.log", params={"id": "tick"})],
    )
    new_text, _diff = render_upsert(yaml_text, tree=tree, location=_on_time_loc(0))
    reparsed = parse_device_yaml(new_text)
    assert reparsed[0].automation.trigger_params == {"seconds": 5}
    assert reparsed[1].automation.trigger_params == {"cron": "0 0 12 * * *"}


def test_upsert_on_time_index_out_of_range_raises() -> None:
    """An index past the append slot is rejected."""
    yaml_text = _load("time_on_time_list.yaml")
    tree = AutomationTree(trigger_id="time.on_time", trigger_params={"hours": 1})
    with pytest.raises(CommandError) as exc:
        render_upsert(yaml_text, tree=tree, location=_on_time_loc(9))
    assert exc.value.code is ErrorCode.INVALID_ARGS


def test_upsert_on_time_with_index_refuses_existing_mapping() -> None:
    """Indexing into a single-mapping on_time is refused, not silently rewritten."""
    yaml_text = (
        "time:\n  - platform: sntp\n    id: my_time\n"
        "    on_time:\n      seconds: 0\n      then:\n        - logger.log: hi\n"
    )
    tree = AutomationTree(trigger_id="time.on_time", trigger_params={"hours": 1})
    with pytest.raises(CommandError) as exc:
        render_upsert(yaml_text, tree=tree, location=_on_time_loc(0))
    assert exc.value.code is ErrorCode.INVALID_ARGS


def test_delete_on_time_entry_keeps_siblings() -> None:
    """Deleting one entry leaves the rest of the list in place."""
    yaml_text = _load("time_on_time_list.yaml")
    new_text, _diff = render_delete(yaml_text, location=_on_time_loc(0))
    reparsed = parse_device_yaml(new_text)
    assert len(reparsed) == 1
    assert reparsed[0].automation.trigger_params == {"cron": "0 0 12 * * *"}


def test_delete_last_on_time_entry_removes_the_key() -> None:
    """Deleting the final entry drops the whole on_time: key."""
    yaml_text = (
        "time:\n  - platform: sntp\n    id: my_time\n"
        "    on_time:\n      - seconds: 0\n        then:\n          - logger.log: hi\n"
    )
    new_text, _diff = render_delete(yaml_text, location=_on_time_loc(0))
    assert "on_time:" not in new_text
    assert parse_device_yaml(new_text) == []


def test_delete_on_time_missing_index_raises_not_found() -> None:
    """Deleting an index that isn't there is a NOT_FOUND."""
    yaml_text = _load("time_on_time_list.yaml")
    with pytest.raises(CommandError) as exc:
        render_delete(yaml_text, location=_on_time_loc(9))
    assert exc.value.code is ErrorCode.NOT_FOUND


def test_upsert_on_time_no_component_block_raises_invalid_args() -> None:
    """Upserting an entry when no time component exists is INVALID_ARGS."""
    tree = AutomationTree(trigger_id="time.on_time", trigger_params={"hours": 1})
    with pytest.raises(CommandError) as exc:
        render_upsert("esphome:\n  name: x\n", tree=tree, location=_on_time_loc(0))
    assert exc.value.code is ErrorCode.INVALID_ARGS


def test_delete_on_time_unknown_component_raises_not_found() -> None:
    """Deleting an entry on an id that isn't configured is NOT_FOUND."""
    yaml_text = _load("time_on_time_list.yaml")
    loc = ComponentOnLocation(component_id="ghost", trigger="on_time", index=0)
    with pytest.raises(CommandError) as exc:
        render_delete(yaml_text, location=loc)
    assert exc.value.code is ErrorCode.NOT_FOUND


_IDLESS_ON_TIME_LIST = (
    "time:\n  - platform: sntp\n"
    "    on_time:\n      - seconds: 0\n        then:\n          - logger.log: hi\n"
)


def test_round_trip_on_time_list_entry_on_idless_instance() -> None:
    """A list-shaped ``on_time`` on an id-less time component (``time_0``) round-trips."""
    parsed = parse_device_yaml(_IDLESS_ON_TIME_LIST)
    assert len(parsed) == 1
    location = parsed[0].location
    assert location.component_id == "time_0"
    new_text, _diff = render_upsert(
        _IDLESS_ON_TIME_LIST, tree=parsed[0].automation, location=location
    )
    reparsed = parse_device_yaml(new_text)
    assert len(reparsed) == 1
    assert reparsed[0].location == location


def test_delete_on_time_list_entry_on_idless_instance() -> None:
    """Deleting the only list-shaped ``on_time`` entry on an id-less time component."""
    location = parse_device_yaml(_IDLESS_ON_TIME_LIST)[0].location
    new_text, _diff = render_delete(_IDLESS_ON_TIME_LIST, location=location)
    assert "on_time:" not in new_text


def test_idless_positional_index_out_of_range_is_refused() -> None:
    """A ``<domain>_<idx>`` id past the end of the section is refused (not mis-written)."""
    with pytest.raises(CommandError):
        render_upsert(
            _IDLESS_BINARY_SENSOR,
            tree=AutomationTree(
                trigger_id="binary_sensor.on_press",
                actions=[ActionNode(action_id="logger.log", params={"format": "hi"})],
            ),
            location=ComponentOnLocation(component_id="binary_sensor_5", trigger="on_press"),
        )


# ---------------------------------------------------------------------------
# Multi-entity sub-entity handlers (#1263)
# ---------------------------------------------------------------------------


_AHT10 = (
    "esphome:\n  name: x\n"
    "sensor:\n"
    "  - platform: aht10\n"
    "    id: aht20\n"
    "    variant: AHT20\n"
    "    temperature:\n"
    "      id: aht20_temperature\n"
    "      name: Kit Temperature\n"
    "    humidity:\n"
    "      id: aht20_humidity\n"
    "      name: Kit Humidity\n"
)


def _value_range_tree() -> AutomationTree:
    return AutomationTree(
        trigger_id="sensor.on_value_range",
        trigger_params={"above": 10, "below": 20},
        actions=[ActionNode(action_id="light.toggle", params={"id": "led"})],
    )


def test_upsert_subentity_splices_under_nested_id() -> None:
    """``on_value_range`` on a sub-sensor lands under its block, not the platform item."""
    new_text, _diff = render_upsert(
        _AHT10,
        tree=_value_range_tree(),
        location=ComponentOnLocation(component_id="aht20_temperature", trigger="on_value_range"),
    )
    # The handler sits between the temperature id and the humidity block — i.e.
    # under temperature, at its child indent, never on the platform item.
    temp_idx = new_text.index("id: aht20_temperature")
    on_idx = new_text.index("on_value_range:")
    hum_idx = new_text.index("id: aht20_humidity")
    assert temp_idx < on_idx < hum_idx, "handler landed outside the temperature block"
    lines = new_text.split("\n")
    # The handler sits at the sub-block child indent (6 spaces), never at the
    # platform item's field indent (4 spaces).
    assert "      on_value_range:" in lines
    assert "    on_value_range:" not in lines


def test_upsert_subentity_leaves_sibling_subsensor_untouched() -> None:
    """Adding a handler to one sub-sensor doesn't disturb the sibling sub-sensor."""
    new_text, _diff = render_upsert(
        _AHT10,
        tree=_value_range_tree(),
        location=ComponentOnLocation(component_id="aht20_temperature", trigger="on_value_range"),
    )
    assert "id: aht20_humidity" in new_text
    assert "name: Kit Humidity" in new_text


def test_round_trip_subentity_handler_is_parsed_back() -> None:
    """A spliced sub-entity handler re-parses to the same sub-entity location."""
    loc = ComponentOnLocation(component_id="aht20_temperature", trigger="on_value_range")
    new_text, _diff = render_upsert(_AHT10, tree=_value_range_tree(), location=loc)
    parsed = parse_device_yaml(new_text)
    assert len(parsed) == 1
    assert parsed[0].location == loc
    assert [a.action_id for a in parsed[0].automation.actions] == ["light.toggle"]


def test_delete_subentity_handler_round_trips_to_original() -> None:
    """Deleting the sub-entity handler restores the byte-identical original."""
    loc = ComponentOnLocation(component_id="aht20_temperature", trigger="on_value_range")
    new_text, _diff = render_upsert(_AHT10, tree=_value_range_tree(), location=loc)
    back, _ddiff = render_delete(new_text, location=loc)
    assert back == _AHT10


def test_two_subsensors_on_one_platform_target_independently() -> None:
    """Handlers on temperature and humidity land in their own blocks."""
    after_temp, _ = render_upsert(
        _AHT10,
        tree=_value_range_tree(),
        location=ComponentOnLocation(component_id="aht20_temperature", trigger="on_value_range"),
    )
    both, _ = render_upsert(
        after_temp,
        tree=_value_range_tree(),
        location=ComponentOnLocation(component_id="aht20_humidity", trigger="on_value_range"),
    )
    targeted = {(p.location.component_id, p.location.trigger) for p in parse_device_yaml(both)}
    assert ("aht20_temperature", "on_value_range") in targeted
    assert ("aht20_humidity", "on_value_range") in targeted


_AHT10_NO_HUMIDITY = (
    "sensor:\n  - platform: aht10\n    id: aht20\n    temperature:\n      id: aht20_temperature\n"
)


def test_upsert_subentity_raises_when_parent_absent() -> None:
    """A sub-entity target whose parent isn't in the YAML can't be spliced."""
    target = ComponentTarget(
        domain="sensor",
        is_sub_entity=True,
        parent_domain="sensor",
        parent_id="ghost",
        sub_key="temperature",
    )
    loc = ComponentOnLocation(component_id="ghost_temperature", trigger="on_value_range")
    with pytest.raises(CommandError):
        _upsert_subentity_on(_AHT10_NO_HUMIDITY, _value_range_tree(), loc, target)


def test_upsert_subentity_raises_when_subblock_absent() -> None:
    """The parent exists but lacks the named sub-block — refuse, don't mis-splice."""
    target = ComponentTarget(
        domain="sensor",
        is_sub_entity=True,
        parent_domain="sensor",
        parent_id="aht20",
        sub_key="humidity",
    )
    loc = ComponentOnLocation(component_id="aht20_humidity", trigger="on_value_range")
    with pytest.raises(CommandError):
        _upsert_subentity_on(_AHT10_NO_HUMIDITY, _value_range_tree(), loc, target)


def test_delete_subentity_raises_when_parent_absent() -> None:
    """Deleting a sub-entity handler off a missing parent is a clean NOT_FOUND."""
    target = ComponentTarget(
        domain="sensor",
        is_sub_entity=True,
        parent_domain="sensor",
        parent_id="ghost",
        sub_key="temperature",
    )
    loc = ComponentOnLocation(component_id="ghost_temperature", trigger="on_value_range")
    with pytest.raises(CommandError):
        _delete_subentity_on(_AHT10_NO_HUMIDITY, loc, target)


def test_subentity_context_rejects_incomplete_target() -> None:
    """A sub-entity target missing its parent context raises rather than leaking None."""
    with pytest.raises(CommandError):
        _subentity_context(ComponentTarget(domain="sensor", is_sub_entity=True))
