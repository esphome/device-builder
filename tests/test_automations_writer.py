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

from esphome_device_builder.controllers.automations.parsing import parse_device_yaml
from esphome_device_builder.controllers.automations.writing import (
    render_delete,
    render_upsert,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models.api import ErrorCode
from esphome_device_builder.models.automations import (
    ActionNode,
    AutomationTree,
    ComponentOnLocation,
    DeviceOnLocation,
    IntervalLocation,
    LightEffectLocation,
    ScriptLocation,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "automation_yamls"


def _load(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


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
    # The lambda survives as a dict under ``id`` (single-arg shortcut).
    src_first = body_first["id"]["_lambda"] if "id" in body_first else body_first["_lambda"]
    src_second = body_second["id"]["_lambda"] if "id" in body_second else body_second["_lambda"]
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
