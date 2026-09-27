"""Tests for refusing an automation write whose location names more than one item."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.automations import AutomationsController, parsing
from esphome_device_builder.controllers.automations.addressing import require_unambiguous
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from esphome_device_builder.models.automations import (
    ComponentOnLocation,
    IntervalLocation,
    ParsedAutomation,
    ScriptLocation,
    YamlDiff,
)

pytestmark = pytest.mark.xdist_group("automations")

_HEAD = "esphome:\n  name: d\n\n"

_SCRIPTS_IDLESS_FIRST = (
    _HEAD
    + "script:\n"
    + "  - then:\n      - logger.log: first\n"
    + "  - id: script_0\n    then:\n      - logger.log: second\n"
)
_SCRIPTS_DECLARED_FIRST = (
    _HEAD
    + "script:\n"
    + "  - id: script_1\n    then:\n      - logger.log: first\n"
    + "  - then:\n      - logger.log: second\n"
)
_BINARY_SENSORS = (
    _HEAD
    + "binary_sensor:\n"
    + "  - platform: gpio\n    pin: GPIO4\n    on_press:\n      - logger.log: pressed\n"
    + "  - platform: gpio\n    pin: GPIO5\n    id: binary_sensor_0\n"
    + "    on_press:\n      - logger.log: pressed\n"
)
_LIGHTS = (
    _HEAD
    + "light:\n"
    + "  - platform: binary\n    output: out_a\n    effects:\n      - strobe:\n"
    + "  - platform: binary\n    output: out_b\n    id: light_0\n"
    + "    effects:\n      - strobe:\n"
)
_ACROSS_DOMAINS = (
    _HEAD
    + "sensor:\n"
    + "  - platform: adc\n    pin: GPIO34\n    id: switch_0\n"
    + "    on_value:\n      - logger.log: value\n"
    + "switch:\n"
    + "  - platform: gpio\n    pin: GPIO4\n    on_turn_on:\n      - logger.log: on\n"
)
_NO_ROW_YET = (
    _HEAD
    + "switch:\n"
    + "  - platform: gpio\n    pin: GPIO4\n"
    + "  - platform: gpio\n    pin: GPIO5\n    id: switch_0\n"
)
_API_ACTIONS = (
    _HEAD
    + "api:\n  actions:\n"
    + "    - action: beep\n      then:\n        - logger.log: one\n"
    + "    - action: beep\n      then:\n        - logger.log: two\n"
)
_UNAMBIGUOUS = (
    _HEAD
    + "script:\n"
    + "  - id: blink\n    then:\n      - logger.log: blink\n"
    + "  - then:\n      - logger.log: no id\n"
    + "binary_sensor:\n"
    + "  - platform: gpio\n    pin: GPIO4\n    on_press:\n      - logger.log: pressed\n"
    + "  - platform: gpio\n    pin: GPIO5\n    id: door\n"
    + "    on_press:\n      - logger.log: door\n"
    + "interval:\n"
    + "  - interval: 1s\n    then:\n      - logger.log: tick\n"
)


class _Devices:
    """Stand-in for the devices controller's locked read-rewrite-save."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.saved: list[str] = []

    async def rewrite_yaml(
        self, configuration: str, rewrite: Callable[[str], tuple[str, YamlDiff]], *, message: str
    ) -> YamlDiff:
        new_text, diff = await asyncio.to_thread(rewrite, self.text)
        self.saved.append(new_text)
        return diff


def _setup(config_dir: Path, text: str) -> tuple[AutomationsController, _Devices]:
    (config_dir / "d.yaml").write_text(text, encoding="utf-8")
    db = MagicMock()
    db.settings.rel_path = config_dir.joinpath
    db.devices = _Devices(text)
    return AutomationsController(db), db.devices


def _rows(text: str) -> list[ParsedAutomation]:
    return parsing.parse_device_yaml(text)


def _write_args(row: ParsedAutomation, *, guarded: bool, save: bool) -> dict[str, Any]:
    args: dict[str, Any] = {"configuration": "d.yaml", "location": row.location.to_dict()}
    if guarded:
        args["expected"] = row.raw_yaml
    if save:
        args["save"] = True
    return args


@pytest.mark.parametrize("guarded", [True, False])
@pytest.mark.parametrize("save", [True, False])
@pytest.mark.parametrize(
    ("text", "name"),
    [
        (_SCRIPTS_IDLESS_FIRST, "script_0"),
        (_SCRIPTS_DECLARED_FIRST, "script_1"),
        (_BINARY_SENSORS, "binary_sensor_0"),
        (_LIGHTS, "light_0"),
        (_ACROSS_DOMAINS, "switch_0"),
        (_API_ACTIONS, "beep"),
    ],
)
async def test_delete_is_refused_for_every_automation_sharing_a_name(
    tmp_path: Path, text: str, name: str, save: bool, guarded: bool
) -> None:
    """A delete aimed at a name two items share is refused for each of them, nothing written."""
    controller, devices = _setup(tmp_path, text)
    rows = _rows(text)
    assert len(rows) == 2

    for row in rows:
        with pytest.raises(CommandError) as excinfo:
            await controller.delete(**_write_args(row, guarded=guarded, save=save))
        assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
        assert f"'{name}' names more than one item" in excinfo.value.message

    assert devices.saved == []


@pytest.mark.parametrize("guarded", [True, False])
@pytest.mark.parametrize(
    "text", [_SCRIPTS_IDLESS_FIRST, _BINARY_SENSORS, _LIGHTS, _ACROSS_DOMAINS, _API_ACTIONS]
)
async def test_replace_is_refused_for_every_automation_sharing_a_name(
    tmp_path: Path, text: str, guarded: bool
) -> None:
    """A replace aimed at a name two items share is refused for each of them, nothing written."""
    controller, devices = _setup(tmp_path, text)

    for row in _rows(text):
        assert row.automation is not None
        with pytest.raises(CommandError) as excinfo:
            await controller.upsert(
                automation=row.automation.to_dict(),
                **_write_args(row, guarded=guarded, save=True),
            )
        assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
        assert "names more than one item" in excinfo.value.message

    assert devices.saved == []


async def test_adding_a_handler_to_a_shared_id_is_refused(tmp_path: Path) -> None:
    """An insert on a component whose id two items share is refused before any row exists."""
    controller, devices = _setup(tmp_path, _NO_ROW_YET)
    automation = {
        "trigger_id": "on_turn_on",
        "trigger_params": {},
        "actions": [
            {"action_id": "delay", "params": {"id": "1s"}, "children": {}, "conditions": []}
        ],
    }

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=automation,
            location={"kind": "component_on", "component_id": "switch_0", "trigger": "on_turn_on"},
            save=True,
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert "'switch_0' names more than one item" in excinfo.value.message
    assert devices.saved == []


async def test_every_automation_with_a_name_of_its_own_is_still_deleted(tmp_path: Path) -> None:
    """Declared ids, the ids of items without one and positions all delete as before."""
    for row in _rows(_UNAMBIGUOUS):
        controller, devices = _setup(tmp_path, _UNAMBIGUOUS)

        await controller.delete(**_write_args(row, guarded=True, save=True))

        assert len(devices.saved) == 1
        assert len(_rows(devices.saved[0])) == len(_rows(_UNAMBIGUOUS)) - 1


async def test_a_config_that_does_not_load_is_left_to_the_writer(tmp_path: Path) -> None:
    """An unguarded delete on a config that does not load answers as the writer does."""
    controller, _devices = _setup(tmp_path, "script: [\n")

    with pytest.raises(CommandError) as excinfo:
        await controller.delete(
            configuration="d.yaml", location={"kind": "script", "id": "script_0"}
        )

    assert "names more than one item" not in excinfo.value.message


def test_a_sub_entity_id_that_another_item_declares_is_shared() -> None:
    """The id made up for a sub-entity counts like any other when another item declares it."""
    text = (
        _HEAD
        + "sensor:\n"
        + "  - platform: dht\n    pin: GPIO4\n    temperature:\n      name: T\n"
        + "  - platform: adc\n    pin: GPIO34\n    id: sensor_0_temperature\n"
    )
    location = ComponentOnLocation(component_id="sensor_0_temperature", trigger="on_value")

    with pytest.raises(CommandError) as excinfo:
        require_unambiguous(text, location)

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED


def test_a_position_and_a_name_nothing_shares_pass() -> None:
    """A location addressed by position, and one whose name a single item has, pass."""
    require_unambiguous(_SCRIPTS_IDLESS_FIRST, IntervalLocation(index=0))
    require_unambiguous(_UNAMBIGUOUS, ScriptLocation(id="blink"))
    require_unambiguous(_UNAMBIGUOUS, ScriptLocation(id="script_1"))
    require_unambiguous(_UNAMBIGUOUS, ScriptLocation(id="not_there"))
