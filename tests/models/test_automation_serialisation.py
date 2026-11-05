"""The automation catalog models omit a field that holds its default, and nothing else."""

from __future__ import annotations

from dataclasses import MISSING, fields

import pytest

from esphome_device_builder.models import ConfigEntry, ConfigEntryType
from esphome_device_builder.models.automations import (
    ActionNode,
    AutomationAction,
    AutomationActionIndex,
    AutomationCondition,
    AutomationConditionIndex,
    AutomationTree,
    AutomationTrigger,
    AutomationTriggerIndex,
    AvailableComponentInstance,
    AvailableScript,
    ChooseBranch,
    ConditionNode,
    Filter,
    FilterIndex,
    LightEffect,
    LightEffectIndex,
    ParsedAutomation,
    ScriptLocation,
)

_ROW = {"id": "x", "name": "X", "description": "", "docs_url": ""}
_DOMAIN_ROW = _ROW | {"domain": "core"}
_REGISTRY_ROW = {"id": "x", "name": "X"}
_MODELS = [
    (AutomationTrigger, _ROW),
    (AutomationAction, _DOMAIN_ROW),
    (AutomationCondition, _DOMAIN_ROW),
    (LightEffect, _REGISTRY_ROW),
    (Filter, _REGISTRY_ROW),
    (AutomationTriggerIndex, _ROW),
    (AutomationActionIndex, _DOMAIN_ROW),
    (AutomationConditionIndex, _DOMAIN_ROW),
    (LightEffectIndex, _REGISTRY_ROW),
    (FilterIndex, _REGISTRY_ROW),
    (AvailableScript, {"id": "blink"}),
    (AvailableComponentInstance, {"component_id": "wifi", "id": "wifi"}),
]


@pytest.mark.parametrize(
    ("model", "required"),
    _MODELS,
    ids=lambda value: value.__name__ if isinstance(value, type) else "",
)
def test_a_model_at_its_defaults_serialises_to_its_required_fields(
    model: type, required: dict[str, str]
) -> None:
    entry = model(**required)

    assert entry.to_dict() == required
    assert model.from_dict(required) == entry


def test_set_flags_and_lists_survive_and_nested_entries_stay_slim() -> None:
    field = ConfigEntry(key="then", type=ConfigEntryType.STRING, label="Then", required=True)
    action = AutomationAction(
        **_DOMAIN_ROW,
        config_entries=[field],
        is_control_flow=True,
        has_condition_gate=True,
        accepts_action_list=["then"],
        scalar_shorthand_key="then",
    )

    wire = action.to_dict()

    assert wire["is_control_flow"] is True
    assert wire["has_condition_gate"] is True
    assert wire["accepts_action_list"] == ["then"]
    assert wire["scalar_shorthand_key"] == "then"
    assert "has_else_branch" not in wire
    assert wire["config_entries"] == [
        {"key": "then", "type": "string", "label": "Then", "required": True}
    ]
    assert AutomationAction.from_dict(wire) == action


def test_a_parsed_automation_omits_a_null_error_and_a_false_unsupported() -> None:
    parsed = ParsedAutomation(
        location=ScriptLocation(id="blink"),
        label="blink",
        automation=AutomationTree(),
        from_line=1,
        to_line=3,
        raw_yaml="script:\n",
    )

    wire = parsed.to_dict()

    assert "error" not in wire and "unsupported" not in wire
    assert wire["automation"] == {"trigger_id": None, "trigger_params": {}, "actions": []}
    assert ParsedAutomation.from_dict(wire) == parsed
    assert ParsedAutomation.from_dict(wire | {"error": "bad", "unsupported": True}).error == "bad"


def test_form_editable_is_sent_only_when_false() -> None:
    assert "form_editable" not in AutomationActionIndex(**_DOMAIN_ROW).to_dict()
    assert (
        AutomationActionIndex(**_DOMAIN_ROW, form_editable=False).to_dict()["form_editable"]
        is False
    )


@pytest.mark.parametrize(
    "model", [m for m, _ in _MODELS] + [ParsedAutomation], ids=lambda model: model.__name__
)
def test_form_editable_is_the_only_truthy_default(model: type) -> None:
    """An absent field reads as false or empty, except the one documented truthy default."""
    truthy = {
        f.name
        for f in fields(model)
        if (f.default is not MISSING and f.default)
        or (f.default_factory is not MISSING and f.default_factory())
    }
    assert truthy == ({"form_editable"} if model is AutomationActionIndex else set())


def test_gate_keys_and_branches_flags_are_sent_when_set() -> None:
    action = AutomationAction(
        **_DOMAIN_ROW, condition_gate_keys=["condition", "any"], has_branches=True
    )

    wire = action.to_dict()

    assert wire["condition_gate_keys"] == ["condition", "any"]
    assert wire["has_branches"] is True
    assert AutomationAction.from_dict(wire) == action


def test_an_action_node_without_branches_on_the_wire_has_none() -> None:
    node = ActionNode.from_dict({"action_id": "delay", "params": {"id": "1s"}})

    assert node.branches == []
    assert node.to_dict()["branches"] == []


def test_a_choose_branch_round_trips() -> None:
    node = ActionNode(
        action_id="choose",
        branches=[
            ChooseBranch(
                conditions=[ConditionNode(condition_id="api.connected")],
                actions=[ActionNode(action_id="delay", params={"id": "1s"})],
            )
        ],
        children={"else": [ActionNode(action_id="logger.log", params={"format": "x"})]},
    )

    wire = node.to_dict()

    assert wire["branches"][0]["conditions"][0]["condition_id"] == "api.connected"
    assert wire["branches"][0]["actions"][0]["action_id"] == "delay"
    assert ActionNode.from_dict(wire) == node
