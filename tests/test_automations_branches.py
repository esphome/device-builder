"""
Branch-coverage tests for the automations package.

The contract tests in ``test_automations_controller.py``,
``test_automations_parse.py``, and ``test_automations_writer.py``
pin the happy paths and the load-bearing error shapes. This file
fills in the remaining defensive / edge-case branches so the
patch hits 100% coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest
from ruamel.yaml.scalarstring import LiteralScalarString

from esphome_device_builder.controllers.automations import catalog
from esphome_device_builder.controllers.automations.controller import (
    _decode_location,
    _scope_from_yaml,
)
from esphome_device_builder.controllers.automations.emitter import (
    dump,
    emit_action_node,
    emit_action_seq,
    emit_condition_node,
    emit_condition_seq,
    emit_effect_item,
    encode_value,
)
from esphome_device_builder.controllers.automations.parsing import (
    _decompose_action,
    _decompose_action_list,
    _decompose_condition,
    _decompose_condition_list,
    _decompose_trigger_mapping,
    _dump_slice,
    _estimate_end_line,
    _is_list_form_trigger,
    _item_range,
    _key_range,
    _render_params,
    _render_value,
    make_yaml,
    parse_device_yaml,
)
from esphome_device_builder.controllers.automations.writing import (
    _indent_for_top_list,
    _locate_top_list_item,
    render_delete,
    render_upsert,
)
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.yaml import (
    _indent_block as _helpers_indent_block,
)
from esphome_device_builder.helpers.yaml import (
    remove_inline_handler,
    upsert_inline_handler,
)
from esphome_device_builder.models.api import ErrorCode
from esphome_device_builder.models.automations import (
    ActionNode,
    AutomationTree,
    ComponentActionFieldLocation,
    ComponentOnLocation,
    ConditionNode,
    DeviceOnLocation,
    IntervalLocation,
    LightEffectLocation,
    ScriptLocation,
)

# ---------------------------------------------------------------------------
# catalog.py
# ---------------------------------------------------------------------------


def test_load_index_returns_empty_when_definitions_missing() -> None:
    """A missing ``automations.index.json`` resolves to empty lists, not a crash."""
    # Call ``__wrapped__`` to bypass ``functools.cache`` without
    # touching the global cache state; a ``cache_clear`` + re-warm
    # would otherwise re-parse the index for the benefit of
    # subsequent tests.
    with patch("esphome_device_builder.controllers.automations.catalog.resources.files") as files:
        files.side_effect = ModuleNotFoundError
        result = catalog._load_index.__wrapped__()
    assert result == {
        "triggers": [],
        "actions": [],
        "conditions": [],
        "light_effects": [],
        "filters": [],
    }


def test_condition_by_id_returns_none_for_unknown_id() -> None:
    """Unknown condition id resolves to ``None`` rather than raising."""
    assert catalog.condition_by_id("not_a_real_condition") is None


def test_light_effect_by_id_returns_none_for_unknown_id() -> None:
    """Unknown light effect id resolves to ``None``."""
    assert catalog.light_effect_by_id("not_a_real_effect") is None


# ---------------------------------------------------------------------------
# controller.py
# ---------------------------------------------------------------------------


def test_scope_from_yaml_malformed_yaml_returns_empty() -> None:
    """A YAML parse error scopes to empty rather than propagating."""
    scoped = _scope_from_yaml(": bad : indent :\n  - not yaml\n  : not yaml\n")
    assert scoped.domains == set()
    assert scoped.scripts == []
    assert scoped.devices == []


def test_scope_from_yaml_top_level_list_returns_empty() -> None:
    """A YAML whose root is a list (not a mapping) scopes to empty."""
    scoped = _scope_from_yaml("- one\n- two\n")
    assert scoped.domains == set()
    assert scoped.scripts == []
    assert scoped.devices == []


def test_scope_skips_script_without_id() -> None:
    """A script list entry missing the required ``id`` key is skipped."""
    scoped = _scope_from_yaml(
        "script:\n  - then:\n      - delay: 1s\n  - id: real\n    then:\n      - delay: 1s\n",
    )
    assert [s.id for s in scoped.scripts] == ["real"]


def test_scope_skips_non_dict_component_instances() -> None:
    """A non-dict entry under a component domain is skipped."""
    scoped = _scope_from_yaml(
        "binary_sensor:\n  - bare_string\n  - platform: gpio\n    id: real\n    pin: GPIO0\n",
    )
    assert [d.id for d in scoped.devices] == ["real"]


def test_scope_surfaces_idless_component_instance() -> None:
    """An id-less leaf surfaces under its positional synthetic id beside ided ones."""
    scoped = _scope_from_yaml(
        "binary_sensor:\n  - platform: gpio\n    pin: GPIO0\n"
        "  - platform: gpio\n    id: real\n    pin: GPIO1\n",
    )
    assert [d.id for d in scoped.devices] == ["binary_sensor_0", "real"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"kind": "script", "id": "alarm"}, ScriptLocation),
        ({"kind": "interval", "index": 0}, IntervalLocation),
        ({"kind": "component_on", "component_id": "b", "trigger": "on_press"}, ComponentOnLocation),
        (
            {"kind": "component_action", "component_id": "g", "field": "open_action"},
            ComponentActionFieldLocation,
        ),
        ({"kind": "device_on", "trigger": "on_boot"}, DeviceOnLocation),
        ({"kind": "light_effect", "component_id": "l", "index": 0}, LightEffectLocation),
    ],
)
def test_decode_location_per_kind(payload: dict, expected: type) -> None:
    """Each ``kind`` discriminator routes to the matching dataclass."""
    assert isinstance(_decode_location(payload), expected)


def test_decode_location_component_on_legacy_payload_defaults_index_none() -> None:
    """A pre-index component_on payload decodes with ``index`` None."""
    loc = _decode_location({"kind": "component_on", "component_id": "b", "trigger": "on_press"})
    assert isinstance(loc, ComponentOnLocation)
    assert loc.index is None


def test_decode_location_component_on_carries_index() -> None:
    """A component_on payload with ``index`` round-trips it."""
    loc = _decode_location(
        {"kind": "component_on", "component_id": "my_time", "trigger": "on_time", "index": 2}
    )
    assert isinstance(loc, ComponentOnLocation)
    assert loc.index == 2


def test_component_on_single_form_omits_index_on_the_wire() -> None:
    """The single-handler form serializes without an ``index`` key."""
    wire = ComponentOnLocation(component_id="b", trigger="on_state").to_dict()
    assert "index" not in wire
    assert wire == {"kind": "component_on", "component_id": "b", "trigger": "on_state"}


def test_component_on_list_form_keeps_index_on_the_wire() -> None:
    """A list-shaped handler keeps its integer ``index`` on the wire."""
    wire = ComponentOnLocation(component_id="my_time", trigger="on_time", index=0).to_dict()
    assert wire["index"] == 0


def test_is_list_form_trigger_discriminates_cron_vs_bare_actions() -> None:
    """A cron entry list is list-form; a bare action list is not."""
    on_time = catalog.trigger_by_id("time.on_time")
    on_press = catalog.trigger_by_id("binary_sensor.on_press")
    assert _is_list_form_trigger([{"seconds": 0, "then": []}], on_time) is True
    assert _is_list_form_trigger([{"then": []}], on_press) is True
    assert _is_list_form_trigger([{"seconds": 0}], on_time) is True
    assert _is_list_form_trigger([{"switch.toggle": "relay"}], on_press) is False
    # An unknown action id is a bare action, not a trigger entry — so its
    # parse error still surfaces instead of being read as trigger params.
    assert _is_list_form_trigger([{"not_a_real_action": 5}], on_press) is False
    assert _is_list_form_trigger(["bare-scalar"], on_time) is False
    assert _is_list_form_trigger([], on_time) is False
    assert _is_list_form_trigger({"seconds": 0}, on_time) is False


def test_decode_location_missing_kind_raises_invalid_args() -> None:
    """A payload without ``kind`` raises INVALID_ARGS."""
    with pytest.raises(CommandError) as err:
        _decode_location({"id": "alarm"})
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_decode_location_unknown_kind_raises_invalid_args() -> None:
    """A payload with an unknown ``kind`` raises INVALID_ARGS."""
    with pytest.raises(CommandError) as err:
        _decode_location({"kind": "bogus"})
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_decode_location_unhashable_kind_raises_invalid_args() -> None:
    """A non-string ``kind`` raises INVALID_ARGS, not TypeError."""
    with pytest.raises(CommandError) as err:
        _decode_location({"kind": []})
    assert err.value.code == ErrorCode.INVALID_ARGS


# ---------------------------------------------------------------------------
# parsing.py
# ---------------------------------------------------------------------------


def test_parse_device_yaml_raises_invalid_args_on_malformed() -> None:
    """Malformed YAML surfaces as INVALID_ARGS."""
    with pytest.raises(CommandError) as err:
        parse_device_yaml(": bad : indent :\n")
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_parse_device_yaml_empty_returns_empty_list() -> None:
    """A YAML that parses to ``None`` (empty file) yields an empty list."""
    assert parse_device_yaml("") == []


def test_parse_skips_unknown_component_trigger_key() -> None:
    """A component ``on_*:`` key that the catalog doesn't know about is skipped."""
    parsed = parse_device_yaml(
        "binary_sensor:\n  - platform: gpio\n    id: b\n    pin: GPIO0\n"
        "    on_never_existed_in_the_catalog:\n      - delay: 1s\n"
        "    on_press:\n      - delay: 1s\n",
    )
    triggers = {p.location.trigger for p in parsed if p.location.kind == "component_on"}
    assert triggers == {"on_press"}


def test_parse_script_skips_non_dict_items() -> None:
    """List items that aren't dicts are silently skipped at the parser level."""
    parsed = parse_device_yaml(
        "script:\n  - bare_string\n  - id: real\n    then:\n      - delay: 1s\n",
    )
    ids = [p.location.id for p in parsed if p.location.kind == "script"]
    assert ids == ["real"]


def test_parse_interval_skips_non_dict_items() -> None:
    """Non-dict interval list items don't surface."""
    parsed = parse_device_yaml(
        "interval:\n  - bare\n  - interval: 30s\n    then:\n      - delay: 1s\n",
    )
    intervals = [p for p in parsed if p.location.kind == "interval"]
    assert len(intervals) == 1


def test_parse_light_skips_non_dict_instances() -> None:
    """Non-dict light instances are skipped."""
    parsed = parse_device_yaml(
        "light:\n  - bare\n"
        "  - platform: binary\n    name: x\n    id: x\n    output: o\n"
        "    effects:\n      - flicker: {}\n",
    )
    effects = [p for p in parsed if p.location.kind == "light_effect"]
    assert len(effects) == 1


def test_decompose_trigger_mapping_explicit_then() -> None:
    """A mapping with ``then:`` splits into trigger params and its action list."""
    tree = _decompose_trigger_mapping(
        {"seconds": 0, "then": [{"delay": "1s"}]}, trigger_id="time.on_time"
    )
    assert tree.trigger_id == "time.on_time"
    assert tree.trigger_params == {"seconds": 0}
    assert [a.action_id for a in tree.actions] == ["delay"]


def test_decompose_trigger_mapping_single_action_shortcut() -> None:
    """The single-action shortcut pulls the action key out of trigger params."""
    tree = _decompose_trigger_mapping(
        {"min_length": "50ms", "logger.log": "hi"}, trigger_id="binary_sensor.on_click"
    )
    assert tree.trigger_params == {"min_length": "50ms"}
    assert [a.action_id for a in tree.actions] == ["logger.log"]


def test_decompose_action_list_returns_empty_for_none() -> None:
    """``None`` body decomposes to an empty action list."""
    assert _decompose_action_list(None) == []


def test_decompose_action_list_skips_empty_and_non_dict_items() -> None:
    """Empty / non-dict list items are skipped without raising."""
    assert _decompose_action_list([{}, "not-a-dict", {"delay": "1s"}]) == [
        ActionNode(action_id="delay", params={"id": "1s"}),
    ]


def test_decompose_action_with_none_params_yields_empty_params() -> None:
    """A registry entry with ``None`` value parses to an action with no params."""
    node = _decompose_action("delay", None)
    assert node.action_id == "delay"
    assert node.params == {}


def test_decompose_action_with_scalar_value_uses_id_shortcut() -> None:
    """A scalar value (``light.turn_on: living_room``) surfaces under ``id``."""
    node = _decompose_action("light.turn_on", "living_room")
    assert node.params == {"id": "living_room"}


def test_decompose_action_scalar_uses_value_shorthand_key() -> None:
    """A value action's scalar lands under its ``maybe`` key, not ``id`` (#bug)."""
    node = _decompose_action("logger.log", "Good morning")
    assert node.params == {"format": "Good morning"}


def test_decompose_action_scalar_falls_back_to_id_for_gate_keyed_shorthand() -> None:
    """``wait_until`` (``maybe == "condition"``) must not put the scalar in params."""
    node = _decompose_action("wait_until", "some_id")
    assert node.params == {"id": "some_id"}


def test_decompose_condition_scalar_uses_value_shorthand_key() -> None:
    """A value condition's scalar lands under its ``maybe`` key."""
    node = _decompose_condition({"display.is_displaying_page": "home_page"})
    assert node.params == {"page_id": "home_page"}


def test_emit_action_multi_param_value_action_has_no_synthetic_id() -> None:
    """Editing a ``logger.log`` (adding a field) never emits a bogus ``id:``."""
    node = ActionNode(action_id="logger.log", params={"format": "x", "level": "INFO"})
    out = dump([emit_action_node(node)])
    assert "format: x" in out and "level: INFO" in out
    assert "id:" not in out


def test_emit_action_single_value_param_collapses_to_shorthand() -> None:
    """A lone shorthand-keyed param re-collapses to the bare-scalar form."""
    node = ActionNode(action_id="logger.log", params={"format": "hi"})
    assert dump([emit_action_node(node)]).strip() == "- logger.log: hi"


def test_emit_action_id_shorthand_collapses() -> None:
    """A maybe_simple_id action (``switch.toggle``) collapses ``id`` to a scalar."""
    node = ActionNode(action_id="switch.toggle", params={"id": "relay"})
    assert dump([emit_action_node(node)]).strip() == "- switch.toggle: relay"


def test_emit_action_bare_scalar_action_collapses_synthetic_id() -> None:
    """``delay`` has no ``id`` field; its synthetic ``id`` param round-trips as a scalar."""
    node = ActionNode(action_id="delay", params={"id": "1s"})
    assert dump([emit_action_node(node)]).strip() == "- delay: 1s"


def test_emit_action_unknown_id_does_not_collapse() -> None:
    """An id absent from the catalog has no known shorthand, so it stays a mapping."""
    node = ActionNode(action_id="not.a_real_action", params={"id": "x"})
    out = dump([emit_action_node(node)])
    assert "id: x" in out
    assert "not.a_real_action: x" not in out


def test_decompose_action_with_children_and_conditions() -> None:
    """``if`` decomposes its ``then`` / ``else`` / ``condition`` keys."""
    node = _decompose_action(
        "if",
        {
            "condition": {"switch.is_on": "r"},
            "then": [{"switch.turn_off": "r"}],
            "else": [{"switch.turn_on": "r"}],
        },
    )
    assert set(node.children) == {"then", "else"}
    assert [c.condition_id for c in node.conditions] == ["switch.is_on"]


def test_decompose_condition_list_returns_empty_for_none() -> None:
    """``None`` decomposes to an empty condition list."""
    assert _decompose_condition_list(None) == []


def test_decompose_condition_list_handles_single_mapping() -> None:
    """A single condition mapping (not wrapped in a list) decomposes cleanly."""
    out = _decompose_condition_list({"switch.is_on": "r"})
    assert len(out) == 1
    assert out[0].condition_id == "switch.is_on"


def test_decompose_condition_list_returns_empty_for_other_types() -> None:
    """A scalar / unexpected type decomposes to an empty list."""
    assert _decompose_condition_list("scalar-not-a-condition") == []


def test_decompose_condition_combinator_with_children() -> None:
    """``and`` decomposes its sub-condition list into ``children``."""
    node = _decompose_condition({"and": [{"switch.is_on": "r1"}, {"switch.is_on": "r2"}]})
    assert node.condition_id == "and"
    assert [c.condition_id for c in node.children] == ["switch.is_on", "switch.is_on"]


def test_decompose_condition_not_single_child() -> None:
    """``not`` decomposes a single-mapping body into one child."""
    node = _decompose_condition({"not": {"switch.is_on": "r"}})
    assert node.condition_id == "not"
    assert [c.condition_id for c in node.children] == ["switch.is_on"]


def test_decompose_condition_not_with_list() -> None:
    """``not`` decomposes a condition list into ``children``."""
    node = _decompose_condition({"not": [{"switch.is_on": "r1"}, {"switch.is_on": "r2"}]})
    assert node.condition_id == "not"
    assert [c.condition_id for c in node.children] == ["switch.is_on", "switch.is_on"]


def test_decompose_condition_leaf_with_dict_params() -> None:
    """A leaf condition with a mapping value surfaces the keys as params."""
    node = _decompose_condition({"for": {"time": "5s", "condition": {"switch.is_on": "r"}}})
    assert node.condition_id == "for"
    assert "time" in node.params


def test_decompose_condition_raises_on_empty_entry() -> None:
    """An empty mapping isn't a valid condition entry."""
    with pytest.raises(CommandError) as err:
        _decompose_condition({})
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_decompose_condition_raises_on_multi_key_entry() -> None:
    """A condition entry must carry exactly one id key."""
    with pytest.raises(CommandError) as err:
        _decompose_condition({"switch.is_on": "r", "switch.is_off": "r"})
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_decompose_condition_raises_on_unknown_id() -> None:
    """Unknown condition ids surface as INVALID_ARGS."""
    with pytest.raises(CommandError) as err:
        _decompose_condition({"made_up_condition": "x"})
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_render_value_passes_lists_through_recursively() -> None:
    """Lists round-trip with each element handled."""
    assert _render_value(["a", 1, {"_lambda": "x;"}]) == ["a", 1, {"_lambda": "x;"}]


def test_render_value_handles_tagged_lambda_scalar() -> None:
    """A ruamel ``!lambda``-tagged plain scalar surfaces as the lambda sentinel."""
    yaml = make_yaml()
    data = yaml.load('a: !lambda ESP_LOGI("x");\n')
    assert _render_value(data["a"]) == {"_lambda": 'ESP_LOGI("x");'}


def test_render_value_falls_back_to_str_for_unknown_tagged_scalar() -> None:
    """A non-``!lambda`` tagged scalar surfaces as its plain string value."""
    yaml = make_yaml()
    data = yaml.load("a: !custom hello\n")
    assert _render_value(data["a"]) == "hello"


def test_render_params_wraps_non_dict_under_value_key() -> None:
    """A bare scalar effect param surfaces under the ``_value`` key."""
    assert _render_params("scalar") == {"_value": "scalar"}


def test_key_range_returns_default_when_lc_absent() -> None:
    """A mapping without ``lc`` metadata returns ``(1, 1)``."""
    assert _key_range({"x": 1}, "x") == (1, 1)


def test_item_range_returns_default_when_lc_absent() -> None:
    """A sequence without ``lc`` metadata returns ``(1, 1)``."""
    assert _item_range(["x"], 0) == (1, 1)


def test_estimate_end_line_returns_start_for_scalar() -> None:
    """A scalar value with no ``lc`` returns the start line."""
    assert _estimate_end_line("scalar", 5) == 5


def test_estimate_end_line_walks_lists_with_lc() -> None:
    """Lists of mappings with ``lc`` metadata bump the end line."""
    yaml = make_yaml()
    data = yaml.load("- key: a\n- key: b\n- key: c\n")
    end = _estimate_end_line(data, 1)
    assert end >= 3


def test_dump_slice_round_trips_mapping() -> None:
    """The dump helper emits standard YAML for a mapping."""
    assert _dump_slice({"a": 1}).strip() == "a: 1"


# ---------------------------------------------------------------------------
# emitter.py
# ---------------------------------------------------------------------------


def test_emit_action_node_bare_action() -> None:
    """A node with no params / children / conditions renders as a bare key."""
    out = emit_action_node(ActionNode(action_id="script.stop"))
    assert out["script.stop"] is None


def test_emit_action_seq_handles_empty_list() -> None:
    """An empty action list emits an empty sequence."""
    seq = emit_action_seq([])
    assert list(seq) == []


def test_emit_condition_seq_emits_list_for_multiple_entries() -> None:
    """A condition list with two entries renders as a YAML sequence."""
    seq = emit_condition_seq(
        [
            ConditionNode(condition_id="switch.is_on", params={"id": "r1"}),
            ConditionNode(condition_id="switch.is_on", params={"id": "r2"}),
        ],
    )
    # Two-entry list → ruamel sequence; one-entry collapses to a mapping.
    assert len(list(seq)) == 2


def test_emit_condition_node_combinator_with_children() -> None:
    """A combinator condition with children emits a nested condition list."""
    out = emit_condition_node(
        ConditionNode(
            condition_id="and",
            children=[
                ConditionNode(condition_id="switch.is_on", params={"id": "r1"}),
                ConditionNode(condition_id="switch.is_on", params={"id": "r2"}),
            ],
        ),
    )
    assert "and" in out
    assert out["and"] is not None


def test_emit_condition_node_not_single_child_collapses() -> None:
    """A ``not`` with one child collapses to ``{not: <mapping>}``."""
    out = emit_condition_node(
        ConditionNode(
            condition_id="not",
            children=[ConditionNode(condition_id="switch.is_on", params={"id": "r"})],
        ),
    )
    assert "switch.is_on" in out["not"]


def test_emit_condition_node_not_multiple_children_emits_seq() -> None:
    """A ``not`` with multiple children emits a nested condition sequence."""
    out = emit_condition_node(
        ConditionNode(
            condition_id="not",
            children=[
                ConditionNode(condition_id="switch.is_on", params={"id": "r1"}),
                ConditionNode(condition_id="switch.is_on", params={"id": "r2"}),
            ],
        ),
    )
    assert isinstance(out["not"], list)
    assert len(out["not"]) == 2


def test_emit_condition_node_bare_condition() -> None:
    """A condition with no params and no children renders as a bare key."""
    out = emit_condition_node(ConditionNode(condition_id="some_condition"))
    assert out["some_condition"] is None


def test_emit_condition_node_id_mapping_field_stays_mapping() -> None:
    """A condition whose sole field is a real ``id`` mapping emits ``id:``, not a scalar."""
    node = ConditionNode(condition_id="time.has_time", params={"id": "id_time"})
    out = dump([emit_condition_node(node)])
    assert "id: id_time" in out
    assert "time.has_time: id_time" not in out


def test_emit_condition_node_value_shorthand_collapses() -> None:
    """A value-shorthand condition still collapses to its bare scalar."""
    node = ConditionNode(condition_id="display.is_displaying_page", params={"page_id": "home"})
    assert dump([emit_condition_node(node)]).strip() == "- display.is_displaying_page: home"


def test_emit_condition_node_with_multi_param_body() -> None:
    """A multi-param condition renders its params under a body mapping."""
    out = emit_condition_node(
        ConditionNode(
            condition_id="for",
            params={"time": "5s", "condition": "x"},
        ),
    )
    body = out["for"]
    assert "time" in body


def test_emit_effect_item_emits_effect_with_params() -> None:
    """Effect rendering wraps params under the effect id."""
    out = emit_effect_item(None, "flicker", {"alpha": 0.95})
    assert "flicker" in out
    body = out["flicker"]
    assert body["alpha"] == 0.95


def test_emit_effect_item_no_params_yields_null_body() -> None:
    """An effect with no params renders as ``<effect_id>: null``."""
    out = emit_effect_item(None, "pulse", {})
    assert out["pulse"] is None


def test_encode_value_lambda_with_trailing_newline_preserved() -> None:
    """A lambda body that already ends with a newline isn't doubled up."""
    out = encode_value({"_lambda": 'ESP_LOGI("x");\n'})
    assert isinstance(out, LiteralScalarString)
    assert str(out) == 'ESP_LOGI("x");\n'


def test_encode_value_recursive_list() -> None:
    """Lists encode element-by-element."""
    out = list(encode_value([1, "two", {"_lambda": "x;"}]))
    assert out[0] == 1
    assert isinstance(out[-1], LiteralScalarString)


def test_encode_value_recursive_dict() -> None:
    """Non-lambda dicts encode key-by-key."""
    out = encode_value({"a": 1, "b": {"_lambda": "x;"}})
    assert out["a"] == 1
    assert isinstance(out["b"], LiteralScalarString)


# ---------------------------------------------------------------------------
# writing.py
# ---------------------------------------------------------------------------


@dataclass
class _UnsupportedLocation:
    """Dummy location subtype used to drive the writer's fallback branch."""

    kind: str = "unsupported"


def test_render_upsert_unsupported_location_raises() -> None:
    """An unrecognised location type raises INVALID_ARGS."""
    with pytest.raises(CommandError) as err:
        render_upsert(
            "esphome:\n  name: x\n",
            tree=AutomationTree(),
            location=_UnsupportedLocation(),  # type: ignore[arg-type]
        )
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_render_delete_unsupported_location_raises() -> None:
    """An unrecognised delete location raises INVALID_ARGS."""
    with pytest.raises(CommandError) as err:
        render_delete(
            "esphome:\n  name: x\n",
            location=_UnsupportedLocation(),  # type: ignore[arg-type]
        )
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_upsert_light_effect_requires_single_key_trigger_params() -> None:
    """``LightEffectLocation`` upsert needs exactly one effect-id key."""
    with pytest.raises(CommandError) as err:
        render_upsert(
            "light:\n  - platform: binary\n    id: x\n    output: o\n",
            tree=AutomationTree(trigger_params={}),
            location=LightEffectLocation(component_id="x", index=0),
        )
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_upsert_light_effect_with_unknown_effect_id_raises() -> None:
    """An unknown effect id raises INVALID_ARGS."""
    with pytest.raises(CommandError) as err:
        render_upsert(
            "light:\n  - platform: binary\n    id: x\n    output: o\n",
            tree=AutomationTree(trigger_params={"never_existed": {}}),
            location=LightEffectLocation(component_id="x", index=0),
        )
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_upsert_light_effect_missing_instance_raises() -> None:
    """A light effect upsert against a missing instance raises INVALID_ARGS."""
    with pytest.raises(CommandError) as err:
        render_upsert(
            "esphome:\n  name: x\n",
            tree=AutomationTree(trigger_params={"flicker": {}}),
            location=LightEffectLocation(component_id="ghost", index=0),
        )
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_upsert_light_effect_appends_effect_to_existing_block() -> None:
    """Adding an effect onto a light with no ``effects:`` block creates the list."""
    text = "light:\n  - platform: binary\n    name: lamp\n    id: lamp\n    output: out\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(trigger_params={"flicker": {"alpha": 0.9}}),
        location=LightEffectLocation(component_id="lamp", index=0),
    )
    assert "effects:" in new_text
    assert "flicker" in new_text


def test_delete_script_when_script_block_missing_raises_not_found() -> None:
    """Deleting a script from a YAML without ``script:`` raises NOT_FOUND."""
    with pytest.raises(CommandError) as err:
        render_delete(
            "esphome:\n  name: x\n",
            location=ScriptLocation(id="absent"),
        )
    assert err.value.code == ErrorCode.NOT_FOUND


def test_delete_device_on_when_handler_missing_raises_not_found() -> None:
    """``esphome:`` present but no ``on_boot:`` → NOT_FOUND on delete."""
    with pytest.raises(CommandError) as err:
        render_delete(
            "esphome:\n  name: x\n",
            location=DeviceOnLocation(trigger="on_boot"),
        )
    assert err.value.code == ErrorCode.NOT_FOUND


def test_delete_light_effect_no_light_block_raises_not_found() -> None:
    """No ``light:`` block → NOT_FOUND."""
    with pytest.raises(CommandError) as err:
        render_delete(
            "esphome:\n  name: x\n",
            location=LightEffectLocation(component_id="any", index=0),
        )
    assert err.value.code == ErrorCode.NOT_FOUND


def test_delete_light_effect_instance_not_found_raises_not_found() -> None:
    """Light block present but no matching id → NOT_FOUND."""
    text = (
        "light:\n  - platform: binary\n    id: real\n    output: o\n"
        "    effects:\n      - flicker: {}\n"
    )
    with pytest.raises(CommandError) as err:
        render_delete(
            text,
            location=LightEffectLocation(component_id="ghost", index=0),
        )
    assert err.value.code == ErrorCode.NOT_FOUND


def test_delete_light_effect_preserves_remaining_effects() -> None:
    """Removing one of two effects leaves the other in place."""
    text = (
        "light:\n  - platform: binary\n    id: lamp\n    output: out\n"
        "    effects:\n      - flicker: {}\n      - pulse: {}\n"
    )
    new_text, _diff = render_delete(
        text,
        location=LightEffectLocation(component_id="lamp", index=0),
    )
    assert "pulse" in new_text
    assert "flicker" not in new_text


def test_upsert_under_top_key_replace_existing_handler_with_sibling_below() -> None:
    """Replacing on_boot when another sibling under ``esphome:`` follows it."""
    text = "esphome:\n  name: x\n  on_boot:\n    then:\n      - delay: 1s\n  area: home\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="on_boot",
            actions=[ActionNode(action_id="delay", params={"id": "5s"})],
        ),
        location=DeviceOnLocation(trigger="on_boot"),
    )
    assert "area: home" in new_text
    assert "delay: 5s" in new_text
    assert "delay: 1s" not in new_text


def test_upsert_under_top_key_insert_skips_trailing_blank_lines() -> None:
    """An esphome block with trailing blanks gains the handler before them."""
    text = "esphome:\n  name: x\n\n\nwifi:\n  ssid: x\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="on_boot",
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=DeviceOnLocation(trigger="on_boot"),
    )
    # The wifi: block is preserved below the new on_boot: handler.
    on_boot_idx = new_text.index("on_boot:")
    wifi_idx = new_text.index("wifi:")
    assert on_boot_idx < wifi_idx


def test_delete_component_on_when_handler_absent_raises_not_found() -> None:
    """A component instance exists but lacks the handler → NOT_FOUND."""
    text = "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
    with pytest.raises(CommandError) as err:
        render_delete(
            text,
            location=ComponentOnLocation(component_id="btn", trigger="on_press"),
        )
    assert err.value.code == ErrorCode.NOT_FOUND


# ---------------------------------------------------------------------------
# helpers/yaml.py
# ---------------------------------------------------------------------------


def test_upsert_inline_handler_replace_with_sibling_below() -> None:
    """An existing handler followed by another key (not EOF) replaces cleanly."""
    text = (
        "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
        "    on_press:\n      then:\n        - delay: 1s\n"
        "    on_release:\n      - delay: 2s\n"
    )
    res = upsert_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
        rendered_yaml="on_press:\n  then:\n    - delay: 99s\n",
    )
    assert res is not None
    new_text, _from, _to, _repl = res
    assert "delay: 99s" in new_text
    # Sibling ``on_release`` survived.
    assert "on_release:" in new_text


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
def test_upsert_inline_handler_locates_quoted_id(quote: str) -> None:
    """A quoted ``id:`` resolves against the unquoted parsed component_id."""
    text = (
        f"binary_sensor:\n  - platform: gpio\n    id: {quote}btn{quote}\n    pin: GPIO0\n"
        "    on_press:\n      then:\n        - delay: 1s\n"
    )
    res = upsert_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
        rendered_yaml="on_press:\n  then:\n    - delay: 99s\n",
    )
    assert res is not None
    new_text, _from, _to, _repl = res
    assert "delay: 99s" in new_text


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
def test_upsert_inline_handler_locates_quoted_dash_line_id(quote: str) -> None:
    """A quoted ``id:`` on the dash line resolves against the parsed component_id."""
    text = (
        f"binary_sensor:\n  - id: {quote}btn{quote}\n    platform: gpio\n    pin: GPIO0\n"
        "    on_press:\n      then:\n        - delay: 1s\n"
    )
    res = upsert_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
        rendered_yaml="on_press:\n  then:\n    - delay: 99s\n",
    )
    assert res is not None
    new_text, _from, _to, _repl = res
    assert "delay: 99s" in new_text


def test_remove_inline_handler_locates_quoted_id() -> None:
    """``remove_inline_handler`` finds an instance whose ``id:`` is quoted."""
    text = (
        'binary_sensor:\n  - platform: gpio\n    id: "btn"\n    pin: GPIO0\n'
        "    on_press:\n      then:\n        - delay: 1s\n"
    )
    res = remove_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
    )
    assert res is not None
    new_text, _from, _to = res
    assert "on_press" not in new_text


def test_upsert_inline_handler_insert_with_trailing_blanks_in_instance() -> None:
    """Trailing blank lines under the component instance don't push insert into them."""
    text = (
        "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
        "\n\n"
        "switch:\n  - platform: gpio\n    id: r\n    pin: GPIO1\n"
    )
    res = upsert_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
        rendered_yaml="on_press:\n  then:\n    - delay: 1s\n",
    )
    assert res is not None
    new_text, _from, _to, _repl = res
    # The switch block is intact below the new on_press handler.
    on_press_idx = new_text.index("on_press:")
    switch_idx = new_text.index("switch:")
    assert on_press_idx < switch_idx


def test_remove_inline_handler_with_sibling_below() -> None:
    """``remove_inline_handler`` stops at the next sibling, not EOF."""
    text = (
        "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
        "    on_press:\n      then:\n        - delay: 1s\n"
        "    on_release:\n      - delay: 2s\n"
    )
    res = remove_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
    )
    assert res is not None
    new_text, _from, _to = res
    assert "on_press" not in new_text
    assert "on_release" in new_text


def test_remove_inline_handler_returns_none_when_handler_absent() -> None:
    """A configured instance without the handler returns ``None``."""
    text = "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
    res = remove_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
    )
    assert res is None


def test_locate_component_instance_returns_none_for_missing_domain() -> None:
    """No ``binary_sensor:`` block at all → upsert returns None."""
    text = "esphome:\n  name: x\n"
    res = upsert_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
        rendered_yaml="on_press: {}",
    )
    assert res is None


def test_locate_component_instance_stops_at_next_top_level_block() -> None:
    """A sibling top-level block ends the domain scan range."""
    # ``switch:`` follows ``binary_sensor:`` — the locator must stop before
    # crossing into it so the inline upsert lands under btn, not under r.
    text = (
        "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
        "switch:\n  - platform: gpio\n    id: r\n    pin: GPIO1\n"
    )
    res = upsert_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
        rendered_yaml="on_press: {}\n",
    )
    assert res is not None
    new_text, _from, _to, _repl = res
    btn_idx = new_text.index("id: btn")
    on_press_idx = new_text.index("on_press:")
    switch_idx = new_text.index("switch:")
    assert btn_idx < on_press_idx < switch_idx


def test_instance_id_match_via_inline_dash_shortcut() -> None:
    """``- id: btn`` on the dash line matches the locator."""
    text = "binary_sensor:\n  - id: btn\n    platform: gpio\n    pin: GPIO0\n"
    res = upsert_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
        rendered_yaml="on_press: {}\n",
    )
    assert res is not None


def test_instance_id_no_match_returns_none() -> None:
    """A domain item without a matching id at any line → no splice."""
    text = (
        "binary_sensor:\n  - platform: gpio\n    pin: GPIO0\n"
        "  - platform: gpio\n    id: real\n    pin: GPIO1\n"
    )
    res = upsert_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="ghost",
        handler_key="on_press",
        rendered_yaml="on_press: {}\n",
    )
    assert res is None


# ---------------------------------------------------------------------------
# More writer branches
# ---------------------------------------------------------------------------


def test_parse_top_level_list_root_returns_empty() -> None:
    """A YAML whose root is a list parses to an empty automations list."""
    assert parse_device_yaml("- one\n- two\n") == []


def test_parse_action_with_non_id_params() -> None:
    """An action with explicit field params surfaces them in ``params``."""
    parsed = parse_device_yaml(
        "esphome:\n  name: x\n  on_boot:\n    then:\n"
        "      - light.turn_on:\n          id: lamp\n          brightness: 50%\n",
    )
    actions = parsed[0].automation.actions
    assert actions[0].params == {"id": "lamp", "brightness": "50%"}


def test_upsert_component_on_with_known_trigger_but_missing_instance_raises() -> None:
    """``binary_sensor.on_press`` against an unknown ``component_id`` raises INVALID_ARGS."""
    text = "binary_sensor:\n  - platform: gpio\n    id: real\n    pin: GPIO0\n"
    with pytest.raises(CommandError) as err:
        render_upsert(
            text,
            tree=AutomationTree(
                trigger_id="binary_sensor.on_press",
                actions=[ActionNode(action_id="delay", params={"id": "1s"})],
            ),
            location=ComponentOnLocation(component_id="ghost", trigger="on_press"),
        )
    assert err.value.code == ErrorCode.INVALID_ARGS


def test_upsert_script_appends_when_id_absent() -> None:
    """A new script id appends a fresh ``- id: ...`` item under ``script:``."""
    new_text, _diff = render_upsert(
        "esphome:\n  name: x\nscript:\n  - id: existing\n    then:\n      - delay: 1s\n",
        tree=AutomationTree(
            trigger_id=None,
            actions=[ActionNode(action_id="delay", params={"id": "2s"})],
        ),
        location=ScriptLocation(id="brand_new"),
    )
    assert "- id: existing" in new_text
    assert "- id: brand_new" in new_text


def test_upsert_script_creates_block_when_absent() -> None:
    """Upserting a script onto a config without ``script:`` creates the block."""
    new_text, _diff = render_upsert(
        "esphome:\n  name: x\n",
        tree=AutomationTree(actions=[ActionNode(action_id="delay", params={"id": "1s"})]),
        location=ScriptLocation(id="alarm"),
    )
    assert "script:" in new_text
    assert "- id: alarm" in new_text


def test_upsert_interval_out_of_range_appends() -> None:
    """An out-of-range interval index appends a fresh item at the end."""
    text = "esphome:\n  name: x\ninterval:\n  - interval: 60s\n    then:\n      - delay: 1s\n"
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_params={"interval": "10s"},
            actions=[ActionNode(action_id="delay", params={"id": "1s"})],
        ),
        location=IntervalLocation(index=99),
    )
    assert new_text.count("- interval:") == 2
    assert "interval: 10s" in new_text


def test_delete_interval_by_index_succeeds() -> None:
    """Deleting an interval by its list index removes that item only."""
    text = (
        "esphome:\n  name: x\n"
        "interval:\n  - interval: 60s\n    then:\n      - delay: 1s\n"
        "  - interval: 30s\n    then:\n      - delay: 2s\n"
    )
    new_text, diff = render_delete(
        text,
        location=IntervalLocation(index=0),
    )
    assert "interval: 60s" not in new_text
    assert "interval: 30s" in new_text
    assert diff.replacement == ""


def test_delete_script_in_multi_script_block_with_unknown_id_raises_not_found() -> None:
    """A script block with other ids but missing the target raises NOT_FOUND."""
    text = "esphome:\n  name: x\nscript:\n  - id: real\n    then:\n      - delay: 1s\n"
    with pytest.raises(CommandError) as err:
        render_delete(text, location=ScriptLocation(id="absent"))
    assert err.value.code == ErrorCode.NOT_FOUND


def test_delete_component_on_returns_diff_with_empty_replacement() -> None:
    """Successful component-on delete returns the spliced range with empty replacement."""
    text = (
        "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
        "    on_press:\n      then:\n        - delay: 1s\n"
    )
    new_text, diff = render_delete(
        text,
        location=ComponentOnLocation(component_id="btn", trigger="on_press"),
    )
    assert "on_press" not in new_text
    assert diff.replacement == ""


def test_delete_light_effect_skips_non_dict_light_instances() -> None:
    """A light list with a non-dict entry doesn't crash the locate step."""
    text = (
        "light:\n  - bare_string\n"
        "  - platform: binary\n    id: lamp\n    output: o\n"
        "    effects:\n      - flicker: {}\n      - pulse: {}\n"
    )
    new_text, _diff = render_delete(
        text,
        location=LightEffectLocation(component_id="lamp", index=0),
    )
    assert "flicker" not in new_text
    assert "pulse" in new_text


def test_upsert_under_top_key_replace_skips_blank_lines_in_walk() -> None:
    """The handler-end walk skips blank lines inside the handler body."""
    text = (
        "esphome:\n  name: x\n"
        "  on_boot:\n"
        "    then:\n"
        "\n"  # blank line inside the handler body
        "      - delay: 1s\n"
        "  area: home\n"
    )
    new_text, _diff = render_upsert(
        text,
        tree=AutomationTree(
            trigger_id="on_boot",
            actions=[ActionNode(action_id="delay", params={"id": "9s"})],
        ),
        location=DeviceOnLocation(trigger="on_boot"),
    )
    assert "area: home" in new_text
    assert "delay: 9s" in new_text


def test_delete_under_top_key_walk_skips_blank_lines() -> None:
    """The delete walk skips blank lines while finding the handler end."""
    text = "esphome:\n  name: x\n  on_boot:\n    then:\n\n      - delay: 1s\n  area: home\n"
    new_text, _diff = render_delete(
        text,
        location=DeviceOnLocation(trigger="on_boot"),
    )
    assert "on_boot" not in new_text
    assert "area: home" in new_text


def test_indent_for_top_list_adds_trailing_newline() -> None:
    """The helper appends a trailing newline when the rendered item lacks one."""
    assert _indent_for_top_list("- foo: bar").endswith("\n")


def test_locate_top_list_item_missing_domain_raises_not_found() -> None:
    """Locating a missing top-level domain raises NOT_FOUND."""
    with pytest.raises(CommandError) as err:
        _locate_top_list_item(["wifi:\n", "  ssid: x\n"], "script", 0)
    assert err.value.code == ErrorCode.NOT_FOUND


def test_locate_top_list_item_index_out_of_range_raises_not_found() -> None:
    """An out-of-range index inside an existing domain raises NOT_FOUND."""
    with pytest.raises(CommandError) as err:
        _locate_top_list_item(
            ["script:\n", "  - id: real\n", "    then: []\n"],
            "script",
            99,
        )
    assert err.value.code == ErrorCode.NOT_FOUND


def test_locate_top_list_item_stops_at_next_top_level_block() -> None:
    """Top-level alphabetic line after the domain ends the search range."""
    start, end = _locate_top_list_item(
        [
            "script:\n",
            "  - id: a\n",
            "    then: []\n",
            "wifi:\n",
            "  ssid: x\n",
        ],
        "script",
        0,
    )
    assert (start, end) == (1, 3)


# ---------------------------------------------------------------------------
# Final coverage gaps
# ---------------------------------------------------------------------------


def test_parse_component_domain_with_non_list_section_is_skipped() -> None:
    """A configured domain that's mis-typed as a scalar doesn't crash."""
    assert parse_device_yaml("binary_sensor: not_a_list\n") == []


def test_parse_light_with_non_list_effects_is_skipped() -> None:
    """A light entry whose ``effects:`` isn't a list is skipped silently."""
    assert (
        parse_device_yaml(
            "light:\n  - platform: binary\n    id: lamp\n    output: o\n    effects: not_a_list\n",
        )
        == []
    )


def test_parse_light_effects_item_must_be_single_key_dict() -> None:
    """An effect entry that isn't a single-key mapping is silently dropped."""
    parsed = parse_device_yaml(
        "light:\n  - platform: binary\n    id: lamp\n    output: o\n"
        "    effects:\n"
        "      - {flicker: {}, pulse: {}}\n"  # two keys → skipped
        "      - flicker: {}\n",
    )
    assert len(parsed) == 1


def test_parse_trigger_with_empty_body_yields_empty_tree() -> None:
    """``on_loop:`` with no body produces an automation with no actions."""
    parsed = parse_device_yaml("esphome:\n  name: x\n  on_loop:\n")
    assert len(parsed) == 1
    tree = parsed[0].automation
    assert tree.actions == []
    assert tree.trigger_params == {}


def test_estimate_end_line_walks_list_of_mappings_with_lc_data() -> None:
    """A list-of-mappings has ``lc.data`` entries that bump the end line."""
    yaml = make_yaml()
    # The inner mapping carries 4-tuple lc entries we read in the
    # second list-data branch.
    data = yaml.load("- a:\n    b: 1\n- a:\n    b: 2\n")
    assert _estimate_end_line(data, 1) >= 3


def test_delete_script_succeeds_when_id_matches() -> None:
    """A successful script delete drops the matching list item."""
    text = (
        "esphome:\n  name: x\n"
        "script:\n  - id: alarm\n    then:\n      - delay: 1s\n"
        "  - id: keep\n    then:\n      - delay: 2s\n"
    )
    new_text, diff = render_delete(text, location=ScriptLocation(id="alarm"))
    assert "- id: alarm" not in new_text
    assert "- id: keep" in new_text
    assert diff.replacement == ""


def test_helpers_indent_block_handles_blank_lines() -> None:
    """``helpers.yaml._indent_block`` preserves embedded blank lines."""
    assert _helpers_indent_block("a\n\nb", "  ") == ["  a", "", "  b"]


def test_upsert_inline_handler_replace_skips_blank_in_walk() -> None:
    """The handler-end walk in ``upsert_inline_handler`` skips blank lines."""
    text = (
        "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
        "    on_press:\n      then:\n\n        - delay: 1s\n"
        "    on_release:\n      - delay: 2s\n"
    )
    res = upsert_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
        rendered_yaml="on_press:\n  then:\n    - delay: 9s\n",
    )
    assert res is not None
    new_text, _from, _to, _repl = res
    assert "delay: 9s" in new_text
    assert "on_release" in new_text


def test_remove_inline_handler_walk_skips_blank() -> None:
    """The remove-handler walk in ``remove_inline_handler`` skips blank lines."""
    text = (
        "binary_sensor:\n  - platform: gpio\n    id: btn\n    pin: GPIO0\n"
        "    on_press:\n      then:\n\n        - delay: 1s\n"
        "    on_release:\n      - delay: 2s\n"
    )
    res = remove_inline_handler(
        text,
        component_domain="binary_sensor",
        component_id="btn",
        handler_key="on_press",
    )
    assert res is not None
    new_text, _from, _to = res
    assert "on_press" not in new_text
    assert "on_release" in new_text
