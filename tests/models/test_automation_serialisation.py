"""The automation catalog models omit a field that holds its default, and nothing else."""

from __future__ import annotations

from dataclasses import MISSING, fields

import pytest

from esphome_device_builder.models import ConfigEntry, ConfigEntryType
from esphome_device_builder.models.automations import (
    AutomationAction,
    AutomationActionIndex,
    AutomationCondition,
    AutomationConditionIndex,
    AutomationTrigger,
    AutomationTriggerIndex,
    Filter,
    FilterIndex,
    LightEffect,
    LightEffectIndex,
)

_ROW = {"id": "x", "name": "X", "description": "", "docs_url": ""}
_DOMAIN_ROW = _ROW | {"domain": "core"}
_REGISTRY_ROW = {"id": "x", "name": "X"}


@pytest.mark.parametrize(
    ("model", "required"),
    [
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
    ],
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
        accepts_action_list=["then"],
        scalar_shorthand_key="then",
    )

    wire = action.to_dict()

    assert wire["is_control_flow"] is True
    assert wire["accepts_action_list"] == ["then"]
    assert wire["scalar_shorthand_key"] == "then"
    assert "has_else_branch" not in wire
    assert wire["config_entries"] == [
        {"key": "then", "type": "string", "label": "Then", "required": True}
    ]
    assert AutomationAction.from_dict(wire) == action


def test_form_editable_is_sent_only_when_false() -> None:
    assert "form_editable" not in AutomationActionIndex(**_DOMAIN_ROW).to_dict()
    assert (
        AutomationActionIndex(**_DOMAIN_ROW, form_editable=False).to_dict()["form_editable"]
        is False
    )


@pytest.mark.parametrize(
    "model",
    [
        AutomationTrigger,
        AutomationAction,
        AutomationCondition,
        LightEffect,
        Filter,
        AutomationTriggerIndex,
        AutomationActionIndex,
        AutomationConditionIndex,
        LightEffectIndex,
        FilterIndex,
    ],
    ids=lambda model: model.__name__,
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
