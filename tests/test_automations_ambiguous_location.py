"""Tests for refusing an automation write whose location names more than one item."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.controllers.automations import AutomationsController, parsing
from esphome_device_builder.controllers.automations.addressing import require_unambiguous
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from esphome_device_builder.models.automations import (
    ApiActionLocation,
    ComponentActionFieldLocation,
    ComponentOnLocation,
    IntervalLocation,
    LightEffectLocation,
    ParsedAutomation,
    ScriptLocation,
)

from .conftest import RecordingAutomationDevices, make_automations_controller

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


_SHARED = [
    pytest.param(_SCRIPTS_IDLESS_FIRST, "script_0", "id", id="script-idless-first"),
    pytest.param(_SCRIPTS_DECLARED_FIRST, "script_1", "id", id="script-declared-first"),
    pytest.param(_BINARY_SENSORS, "binary_sensor_0", "id", id="component-handler"),
    pytest.param(_LIGHTS, "light_0", "id", id="light-effect"),
    pytest.param(_ACROSS_DOMAINS, "switch_0", "id", id="across-domains"),
    pytest.param(_API_ACTIONS, "beep", "action name", id="api-action"),
]
_AUTOMATION = {
    "trigger_id": None,
    "trigger_params": {},
    "actions": [{"action_id": "delay", "params": {"id": "1s"}, "children": {}, "conditions": []}],
}


def _setup(config_dir: Path, text: str) -> tuple[AutomationsController, RecordingAutomationDevices]:
    devices = RecordingAutomationDevices(text)
    return make_automations_controller(config_dir, text, devices=devices), devices


async def _rows(text: str) -> list[ParsedAutomation]:
    return await asyncio.to_thread(parsing.parse_device_yaml, text)


def _write_args(row: ParsedAutomation, *, guarded: bool, save: bool = True) -> dict[str, Any]:
    args: dict[str, Any] = {"configuration": "d.yaml", "location": row.location.to_dict()}
    if guarded:
        args["expected"] = row.raw_yaml
    if save:
        args["save"] = True
    return args


def _refused(excinfo: pytest.ExceptionInfo[CommandError], name: str, what: str) -> None:
    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert f"more than one item in this config is named '{name}'" in excinfo.value.message
    assert f"its own {what}" in excinfo.value.message


@pytest.mark.parametrize("guarded", [True, False])
@pytest.mark.parametrize("save", [True, False])
@pytest.mark.parametrize(("text", "name", "what"), _SHARED)
async def test_delete_is_refused_for_every_automation_sharing_a_name(
    tmp_path: Path, text: str, name: str, what: str, save: bool, guarded: bool
) -> None:
    """A delete aimed at a name two items share is refused for each of them, nothing written."""
    controller, devices = _setup(tmp_path, text)
    rows = await _rows(text)
    assert len(rows) == 2

    for row in rows:
        with pytest.raises(CommandError) as excinfo:
            await controller.delete(**_write_args(row, guarded=guarded, save=save))
        _refused(excinfo, name, what)

    assert devices.saved == []


@pytest.mark.parametrize("guarded", [True, False])
@pytest.mark.parametrize(("text", "name", "what"), _SHARED)
async def test_replace_is_refused_for_every_automation_sharing_a_name(
    tmp_path: Path, text: str, name: str, what: str, guarded: bool
) -> None:
    """A replace aimed at a name two items share is refused for each of them, nothing written."""
    controller, devices = _setup(tmp_path, text)
    rows = await _rows(text)
    assert len(rows) == 2

    for row in rows:
        assert row.automation is not None
        with pytest.raises(CommandError) as excinfo:
            await controller.upsert(
                automation=row.automation.to_dict(), **_write_args(row, guarded=guarded)
            )
        _refused(excinfo, name, what)

    assert devices.saved == []


async def test_adding_a_handler_to_a_shared_id_is_refused(tmp_path: Path) -> None:
    """An insert on a component whose id two items share is refused before any row exists."""
    controller, devices = _setup(tmp_path, _NO_ROW_YET)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_AUTOMATION | {"trigger_id": "on_turn_on"},
            location={"kind": "component_on", "component_id": "switch_0", "trigger": "on_turn_on"},
            save=True,
        )

    _refused(excinfo, "switch_0", "id")
    assert devices.saved == []


async def test_every_automation_with_a_name_of_its_own_is_still_deleted(tmp_path: Path) -> None:
    """Declared ids, the ids of items without one and positions all delete their own row."""
    rows = await _rows(_UNAMBIGUOUS)
    for row in rows:
        controller, devices = _setup(tmp_path, _UNAMBIGUOUS)

        await controller.delete(**_write_args(row, guarded=True))

        [(_configuration, saved, _message)] = devices.saved
        left = [kept.raw_yaml for kept in await _rows(saved)]
        assert left == [other.raw_yaml for other in rows if other is not row]


async def test_a_config_that_does_not_load_is_left_to_the_writer(tmp_path: Path) -> None:
    """An unguarded delete on a config that does not load answers as the writer does."""
    controller, _devices = _setup(tmp_path, "switch: [\n")

    with pytest.raises(CommandError) as excinfo:
        await controller.delete(
            configuration="d.yaml",
            location={"kind": "component_on", "component_id": "switch_0", "trigger": "on_turn_on"},
        )

    assert excinfo.value.code is not ErrorCode.PRECONDITION_FAILED


async def test_adding_a_script_under_the_listed_id_of_one_without_is_refused(
    tmp_path: Path,
) -> None:
    """An unguarded write to the id a script without one is listed under leaves that script."""
    controller, _devices = _setup(tmp_path, _UNAMBIGUOUS)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_AUTOMATION,
            location={"kind": "script", "id": "script_1"},
            yaml=_UNAMBIGUOUS,
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert "'script_1' is the id a script without an id is listed under" in excinfo.value.message


@pytest.mark.parametrize("script_id", ["blink", "script_5"])
async def test_a_script_is_written_under_a_declared_or_a_free_id(
    tmp_path: Path, script_id: str
) -> None:
    """An unguarded write replaces a script that declares the id and adds one under a free id."""
    controller, _devices = _setup(tmp_path, _UNAMBIGUOUS)

    result = await controller.upsert(
        configuration="d.yaml",
        automation=_AUTOMATION,
        location={"kind": "script", "id": script_id},
        yaml=_UNAMBIGUOUS,
    )

    assert f"id: {script_id}" in result["yaml_diff"]["replacement"]


async def test_a_script_without_an_id_is_replaced_when_its_text_is_given(tmp_path: Path) -> None:
    """A replace that carries the listed text of a script without an id still lands on it."""
    controller, devices = _setup(tmp_path, _UNAMBIGUOUS)
    script = ScriptLocation("script_1")
    shown = next(row for row in await _rows(_UNAMBIGUOUS) if row.location == script)

    await controller.upsert(automation=_AUTOMATION, **_write_args(shown, guarded=True))

    [(_configuration, saved, _message)] = devices.saved
    assert "id: script_1" in saved
    assert "no id" not in saved


def test_an_action_field_on_a_shared_id_is_refused() -> None:
    """A location that addresses an action field by a shared component id is refused."""
    location = ComponentActionFieldLocation(component_id="binary_sensor_0", field="on_press")

    with pytest.raises(CommandError) as excinfo:
        require_unambiguous(_BINARY_SENSORS, location)

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED


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


def test_a_light_effect_is_shared_among_lights_only() -> None:
    """A light's effects are found among the lights, so an id another domain declares passes."""
    text = (
        _HEAD
        + "light:\n"
        + "  - platform: binary\n    output: light_0\n    effects:\n      - strobe:\n"
        + "output:\n"
        + "  - platform: template\n    id: light_0\n    type: binary\n"
    )

    require_unambiguous(text, LightEffectLocation(component_id="light_0", index=0))


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_HEAD, id="no-api"),
        pytest.param(_HEAD + "api:\n", id="bare-api"),
        pytest.param(_HEAD + "api:\n  port: 6053\n", id="no-actions"),
        pytest.param(
            _API_ACTIONS.replace("actions:", "services:").replace("action:", "service:", 1),
            id="one-service-one-action",
        ),
    ],
)
def test_an_api_action_is_counted_where_the_parser_finds_them(text: str) -> None:
    """Actions are counted under ``actions:`` or the legacy ``services:``, by either key."""
    location = ApiActionLocation(action_name="beep")

    if "beep" not in text:
        require_unambiguous(text, location)
        return
    with pytest.raises(CommandError) as excinfo:
        require_unambiguous(text, location)
    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED


def test_a_position_and_a_name_nothing_shares_pass() -> None:
    """A location addressed by position, and one whose name a single item has, pass."""
    require_unambiguous(_SCRIPTS_IDLESS_FIRST, IntervalLocation(index=0))
    require_unambiguous(_UNAMBIGUOUS, ScriptLocation(id="blink"))
    require_unambiguous(_UNAMBIGUOUS, ScriptLocation(id="script_1"))
    require_unambiguous(_UNAMBIGUOUS, ScriptLocation(id="not_there"))
    require_unambiguous("switch: [\n", ScriptLocation(id="script_0"))
