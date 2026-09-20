"""``automations/upsert`` refuses a tree the catalog cannot render."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.automations import AutomationsController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

pytestmark = pytest.mark.xdist_group("automations")

_YAML = "esphome:\n  name: d\n"
_LOCATION = {"kind": "device_on", "trigger": "on_boot"}


def _controller(config_dir: Path) -> AutomationsController:
    (config_dir / "d.yaml").write_text(_YAML, encoding="utf-8")
    db = MagicMock()
    db.settings.rel_path = config_dir.joinpath
    return AutomationsController(db)


def _tree(*actions: dict[str, Any]) -> dict[str, Any]:
    return {"trigger_id": "on_boot", "trigger_params": {}, "actions": list(actions)}


def _action(action_id: str, params: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"action_id": action_id, "params": params, "children": {}, "conditions": []} | extra


def _cond(condition_id: str, params: dict[str, Any], *children: dict[str, Any]) -> dict[str, Any]:
    return {"condition_id": condition_id, "params": params, "children": list(children)}


async def test_a_field_the_catalog_does_not_list_is_refused_naming_the_real_ones(
    tmp_path: Path,
) -> None:
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_tree(_action("switch.turn_off", {"switch_id": "relay"})),
            location=_LOCATION,
        )

    assert excinfo.value.code is ErrorCode.INVALID_ARGS
    assert excinfo.value.message == (
        "'switch.turn_off' has no field ['switch_id']; its fields are ['id']"
    )


@pytest.mark.parametrize(
    ("actions", "fragment"),
    [
        ([_action("switch.explode", {"id": "relay"})], "Unknown action id 'switch.explode'"),
        ([_action("delay", {"id": "1s"}, children={"then": []})], "has no ['then'] branch"),
        ([_action("if", {"then": []})], "'if' has no field ['then']; its fields are ['id']"),
        ([_action("delay", {"condition": {}})], "'delay' has no field ['condition']"),
        ([_action("logger.log", {"id": "tick"})], "'logger.log' has no field ['id']"),
        ([_action("script.stop", {"id": "s", "times": 3})], "'script.stop' has no field ['times']"),
        (
            [_action("if", {}, children={"then": [_action("switch.turn_on", {"nope": 1})]})],
            "'switch.turn_on' has no field ['nope']",
        ),
        (
            [
                _action(
                    "if",
                    {},
                    conditions=[
                        {
                            "condition_id": "binary_sensor.is_on",
                            "params": {"pin": 4},
                            "children": [],
                        }
                    ],
                )
            ],
            "'binary_sensor.is_on' has no field ['pin']",
        ),
        (
            [
                _action(
                    "if",
                    {},
                    conditions=[{"condition_id": "sensor.is_hot", "params": {}, "children": []}],
                )
            ],
            "Unknown condition id 'sensor.is_hot'",
        ),
        (
            [
                _action(
                    "if",
                    {},
                    conditions=[
                        {
                            "condition_id": "binary_sensor.is_on",
                            "params": {"id": "b"},
                            "children": [
                                {
                                    "condition_id": "lambda",
                                    "params": {"lambda": "x"},
                                    "children": [],
                                }
                            ],
                        }
                    ],
                )
            ],
            "takes no nested conditions",
        ),
    ],
    ids=[
        "unknown_action",
        "stray_branch",
        "branch_as_param",
        "gate_as_param",
        "shorthand_not_id",
        "script_stop_extra",
        "nested_child",
        "condition_field",
        "unknown_condition",
        "nested_condition",
    ],
)
async def test_a_tree_the_catalog_cannot_render_is_refused(
    tmp_path: Path, actions: list[dict[str, Any]], fragment: str
) -> None:
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml", automation=_tree(*actions), location=_LOCATION
        )

    assert excinfo.value.code is ErrorCode.INVALID_ARGS
    assert fragment in excinfo.value.message


async def test_a_valid_tree_with_shorthand_branches_and_conditions_renders(tmp_path: Path) -> None:
    controller = _controller(tmp_path)
    tree = _tree(
        _action("delay", {"id": "1s"}),
        _action(
            "if",
            {},
            children={"then": [_action("switch.turn_off", {"id": "relay"})], "else": []},
            conditions=[
                {
                    "condition_id": "and",
                    "params": {},
                    "children": [
                        {
                            "condition_id": "binary_sensor.is_on",
                            "params": {"id": "b"},
                            "children": [],
                        }
                    ],
                }
            ],
        ),
        _action("external.thing", {"anything": 1}, unknown=True, raw_body={"anything": 1}),
    )

    result = await controller.upsert(configuration="d.yaml", automation=tree, location=_LOCATION)

    assert "switch.turn_off: relay" in result["yaml_diff"]["replacement"]


@pytest.mark.parametrize(
    "actions",
    [
        [
            _action(
                "if",
                {},
                conditions=[_cond("for", {"time": "5s", "condition": {"switch.is_on": "r"}})],
            )
        ],
        [_action("wait_until", {"id": "x"})],
        [_action("script.execute", {"id": "blink", "times": 3, "colour": "red"})],
        [_action("lvgl.label.update", {"id": "lbl", "text": "hi"})],
    ],
    ids=["for_condition_gate", "wait_until_scalar", "script_parameters", "known_not_editable"],
)
async def test_what_the_parser_produces_is_accepted(
    tmp_path: Path, actions: list[dict[str, Any]]
) -> None:
    controller = _controller(tmp_path)

    result = await controller.upsert(
        configuration="d.yaml", automation=_tree(*actions), location=_LOCATION
    )

    assert result["yaml_diff"]["replacement"]
