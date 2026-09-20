"""Tests for the ``save`` and ``expected`` options of ``automations/delete`` and ``upsert``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from esphome_device_builder.controllers.automations import AutomationsController, parsing
from esphome_device_builder.controllers.automations import controller as automations_controller
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from esphome_device_builder.models.automations import YamlDiff

pytestmark = pytest.mark.xdist_group("automations")

_YAML = "esphome:\n  name: d\n  on_boot:\n    then:\n      - delay: 1s\n"
_LOCATION = {"kind": "device_on", "trigger": "on_boot"}
_AUTOMATION = {
    "trigger_id": "on_boot",
    "trigger_params": {},
    "actions": [
        {"action_id": "delay", "params": {"id": "1s"}, "children": {}, "conditions": []},
    ],
}


class _Devices:
    """Stand-in for the devices controller's locked read-rewrite-save."""

    def __init__(self, text: str = _YAML) -> None:
        self.text = text
        self.saved: list[tuple[str, str, str]] = []

    async def rewrite_yaml(
        self, configuration: str, rewrite: Callable[[str], tuple[str, YamlDiff]], *, message: str
    ) -> YamlDiff:
        new_text, diff = await asyncio.to_thread(rewrite, self.text)
        self.saved.append((configuration, new_text, message))
        return diff


def _make_controller(config_dir: Path, *, devices: Any) -> AutomationsController:
    (config_dir / "d.yaml").write_text(_YAML, encoding="utf-8")
    db = MagicMock()
    db.settings.rel_path = config_dir.joinpath
    db.devices = devices
    return AutomationsController(db)


@pytest.mark.parametrize("save", [True, False])
async def test_delete_writes_the_spliced_config_only_with_save(tmp_path: Path, save: bool) -> None:
    devices = _Devices()
    controller = _make_controller(tmp_path, devices=devices)

    result = await controller.delete(configuration="d.yaml", location=_LOCATION, save=save)

    assert result["yaml_diff"] == {"fromLine": 3, "toLine": 5, "replacement": ""}
    saved = [("d.yaml", "esphome:\n  name: d\n", "Delete an automation from d.yaml")]
    assert devices.saved == (saved if save else [])


async def test_delete_with_expected_removes_only_the_automation_it_was_shown(
    tmp_path: Path,
) -> None:
    devices = _Devices()
    controller = _make_controller(tmp_path, devices=devices)
    shown = (await asyncio.to_thread(parsing.parse_device_yaml, _YAML))[0].raw_yaml
    assert shown.endswith("\n")

    result = await controller.delete(
        configuration="d.yaml", location=_LOCATION, save=True, expected=shown.rstrip("\n")
    )

    assert result["yaml_diff"] == {"fromLine": 3, "toLine": 5, "replacement": ""}
    saved = [("d.yaml", "esphome:\n  name: d\n", "Delete an automation from d.yaml")]
    assert devices.saved == saved


@pytest.mark.parametrize(
    ("location", "expected", "fragment"),
    [
        (
            _LOCATION,
            "on_boot:\n  then:\n    - delay: 2s\n",
            "differs from the expected text",
        ),
        (
            {"kind": "device_on", "trigger": "on_shutdown"},
            "on_shutdown: {}\n",
            "no automation at that location any more",
        ),
    ],
    ids=["changed", "moved"],
)
async def test_delete_with_expected_refuses_a_changed_or_missing_automation(
    tmp_path: Path, location: dict[str, Any], expected: str, fragment: str
) -> None:
    devices = _Devices()
    controller = _make_controller(tmp_path, devices=devices)

    with pytest.raises(CommandError) as excinfo:
        await controller.delete(
            configuration="d.yaml", location=location, save=True, expected=expected
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert fragment in excinfo.value.message
    if fragment.startswith("differs"):
        assert "nothing was deleted, list again before deleting" in excinfo.value.message
        assert "-    - delay: 2s\n+    - delay: 1s\n" in excinfo.value.message
    assert devices.saved == []


async def test_delete_with_expected_refuses_a_positional_index_that_shifted(
    tmp_path: Path,
) -> None:
    item = "  - interval: {n}s\n    then:\n      - delay: {n}s\n"
    listed = "interval:\n" + item.format(n=1)
    on_disk = "interval:\n" + item.format(n=9) + item.format(n=1)
    devices = _Devices(on_disk)
    controller = _make_controller(tmp_path, devices=devices)
    shown = (await asyncio.to_thread(parsing.parse_device_yaml, listed))[0].raw_yaml

    with pytest.raises(CommandError) as excinfo:
        await controller.delete(
            configuration="d.yaml",
            location={"kind": "interval", "index": 0},
            save=True,
            expected=shown,
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert devices.saved == []


async def test_delete_with_expected_refuses_a_file_that_no_longer_loads(tmp_path: Path) -> None:
    devices = _Devices("esphome: [\n")
    controller = _make_controller(tmp_path, devices=devices)

    with pytest.raises(CommandError) as excinfo:
        await controller.delete(
            configuration="d.yaml", location=_LOCATION, save=True, expected="on_boot: {}\n"
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert excinfo.value.message.startswith("the config no longer loads, nothing was deleted: ")
    assert isinstance(excinfo.value.__cause__, CommandError)
    assert devices.saved == []


async def test_delete_with_expected_passes_other_parser_errors_through(tmp_path: Path) -> None:
    devices = _Devices()
    controller = _make_controller(tmp_path, devices=devices)
    other = CommandError(ErrorCode.UNAVAILABLE, "catalog not loaded")

    with (
        patch.object(automations_controller.parsing, "parse_device_yaml", side_effect=other),
        pytest.raises(CommandError) as excinfo,
    ):
        await controller.delete(
            configuration="d.yaml", location=_LOCATION, save=True, expected="on_boot: {}\n"
        )

    assert excinfo.value is other
    assert devices.saved == []


async def test_delete_refuses_a_non_string_expected(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, devices=_Devices())

    with pytest.raises(CommandError) as excinfo:
        await controller.delete(
            configuration="d.yaml",
            location=_LOCATION,
            expected=7,  # type: ignore[arg-type]
        )

    assert excinfo.value.code is ErrorCode.INVALID_ARGS
    assert "expected must be a string" in excinfo.value.message


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"save": True, "yaml": _YAML}, id="save_with_a_draft"),
        pytest.param({"save": "yes"}, id="non_boolean_save"),
    ],
)
async def test_delete_refuses_invalid_save_args(tmp_path: Path, args: dict[str, Any]) -> None:
    devices = _Devices()
    controller = _make_controller(tmp_path, devices=devices)

    with pytest.raises(CommandError) as err:
        await controller.delete(configuration="d.yaml", location=_LOCATION, **args)

    assert err.value.code == ErrorCode.INVALID_ARGS
    assert devices.saved == []


async def test_delete_with_save_needs_the_devices_controller(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, devices=None)

    with pytest.raises(CommandError) as err:
        await controller.delete(configuration="d.yaml", location=_LOCATION, save=True)

    assert err.value.code == ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize("yaml", [None, _YAML], ids=["from_disk", "from_a_draft"])
async def test_a_config_read_and_its_processing_share_one_executor_job(
    tmp_path: Path, yaml: str | None
) -> None:
    controller = _make_controller(tmp_path, devices=None)

    with patch.object(
        automations_controller, "run_in_executor", wraps=automations_controller.run_in_executor
    ) as spy:
        parsed = await controller.parse(configuration="d.yaml", yaml=yaml)

    assert len(parsed) == 1
    assert spy.await_count == 1


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("get_available", {}),
        ("parse", {}),
        ("upsert", {"location": _LOCATION, "automation": _AUTOMATION}),
        ("delete", {"location": _LOCATION}),
    ],
)
async def test_a_missing_config_is_not_found(
    tmp_path: Path, command: str, args: dict[str, Any]
) -> None:
    controller = _make_controller(tmp_path, devices=None)

    with pytest.raises(CommandError) as err:
        await getattr(controller, command)(configuration="ghost.yaml", **args)

    assert err.value.code == ErrorCode.NOT_FOUND


_REPLACEMENT = {
    "trigger_id": "on_boot",
    "trigger_params": {},
    "actions": [
        {"action_id": "delay", "params": {"id": "2s"}, "children": {}, "conditions": []},
    ],
}


async def test_upsert_with_save_inserts_at_an_empty_location(tmp_path: Path) -> None:
    devices = _Devices()
    controller = _make_controller(tmp_path, devices=devices)

    result = await controller.upsert(
        configuration="d.yaml",
        automation=_AUTOMATION,
        location={"kind": "device_on", "trigger": "on_shutdown"},
        save=True,
    )

    assert "on_shutdown" in result["yaml_diff"]["replacement"]
    (configuration, new_text, message) = devices.saved[0]
    assert (configuration, message) == ("d.yaml", "Save an automation to d.yaml")
    assert "on_shutdown:" in new_text and "on_boot:" in new_text


async def test_upsert_with_save_refuses_to_replace_without_expected(tmp_path: Path) -> None:
    devices = _Devices()
    controller = _make_controller(tmp_path, devices=devices)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml", automation=_REPLACEMENT, location=_LOCATION, save=True
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert "already holds YAML" in excinfo.value.message
    assert devices.saved == []


async def test_upsert_with_save_refuses_to_replace_a_list_shaped_handler(tmp_path: Path) -> None:
    handlers = "    - then:\n        - delay: 9s\n    - then:\n        - delay: 8s\n"
    listed = "esphome:\n  name: d\n  on_boot:\n" + handlers
    devices = _Devices(listed)
    controller = _make_controller(tmp_path, devices=devices)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml", automation=_REPLACEMENT, location=_LOCATION, save=True
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert devices.saved == []


async def test_upsert_with_save_appends_a_handler_to_a_list_shaped_trigger(
    tmp_path: Path,
) -> None:
    handlers = "    - then:\n        - delay: 9s\n    - then:\n        - delay: 8s\n"
    devices = _Devices("esphome:\n  name: d\n  on_boot:\n" + handlers)
    controller = _make_controller(tmp_path, devices=devices)

    await controller.upsert(
        configuration="d.yaml",
        automation=_AUTOMATION,
        location={"kind": "device_on", "trigger": "on_boot", "index": 2},
        save=True,
    )

    assert devices.saved[0][1].count("- then:") == 3


async def test_upsert_with_save_appends_to_a_bare_action_list_shorthand(tmp_path: Path) -> None:
    shorthand = (
        "button:\n  - platform: template\n    name: B\n    id: bid\n"
        "    on_press:\n      - switch.turn_off: relay\n"
    )
    devices = _Devices(shorthand)
    controller = _make_controller(tmp_path, devices=devices)

    await controller.upsert(
        configuration="d.yaml",
        automation=_AUTOMATION | {"trigger_id": None},
        location={"kind": "component_on", "component_id": "bid", "trigger": "on_press", "index": 1},
        save=True,
    )

    saved = devices.saved[0][1]
    assert saved.count("- then:") == 2 and "switch.turn_off: relay" in saved


async def test_upsert_with_save_refuses_an_index_the_writer_cannot_honour(tmp_path: Path) -> None:
    devices = _Devices("interval:\n  - interval: 1s\n    then:\n      - delay: 1s\n")
    controller = _make_controller(tmp_path, devices=devices)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_AUTOMATION | {"trigger_id": None, "trigger_params": {"interval": "5s"}},
            location={"kind": "interval", "index": 5},
            save=True,
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert devices.saved == []


async def test_upsert_with_save_appends_to_a_list_without_expected(tmp_path: Path) -> None:
    devices = _Devices("interval:\n  - interval: 1s\n    then:\n      - delay: 1s\n")
    controller = _make_controller(tmp_path, devices=devices)

    result = await controller.upsert(
        configuration="d.yaml",
        automation=_AUTOMATION | {"trigger_id": None, "trigger_params": {"interval": "5s"}},
        location={"kind": "interval", "index": 1},
        save=True,
    )

    assert result["yaml_diff"]["toLine"] == result["yaml_diff"]["fromLine"] - 1
    assert devices.saved[0][1].count("- interval:") == 2


async def test_upsert_with_save_inserts_into_a_legacy_api_services_block(tmp_path: Path) -> None:
    devices = _Devices("api:\n  services:\n    - service: ping\n      then:\n        - delay: 1s\n")
    controller = _make_controller(tmp_path, devices=devices)

    await controller.upsert(
        configuration="d.yaml",
        automation=_AUTOMATION | {"trigger_id": None},
        location={"kind": "api_action", "action_name": "pong"},
        save=True,
    )

    assert devices.saved[0][1].count("- action:") == 2


async def test_upsert_with_expected_refuses_when_the_writer_would_append_instead(
    tmp_path: Path,
) -> None:
    idless = "script:\n  - then:\n      - delay: 1s\n"
    devices = _Devices(idless)
    controller = _make_controller(tmp_path, devices=devices)
    shown = (await asyncio.to_thread(parsing.parse_device_yaml, idless))[0]

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_AUTOMATION | {"trigger_id": None},
            location=shown.location.to_dict(),
            save=True,
            expected=shown.raw_yaml,
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert "could not be replaced in place" in excinfo.value.message
    assert devices.saved == []


async def test_upsert_with_save_refuses_a_file_that_no_longer_loads(tmp_path: Path) -> None:
    devices = _Devices("esphome: [\n")
    controller = _make_controller(tmp_path, devices=devices)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_AUTOMATION,
            location={"kind": "device_on", "trigger": "on_shutdown"},
            save=True,
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert devices.saved == []


async def test_upsert_with_expected_replaces_the_automation_it_was_shown(tmp_path: Path) -> None:
    devices = _Devices()
    controller = _make_controller(tmp_path, devices=devices)
    shown = (await asyncio.to_thread(parsing.parse_device_yaml, _YAML))[0].raw_yaml

    await controller.upsert(
        configuration="d.yaml",
        automation=_REPLACEMENT,
        location=_LOCATION,
        save=True,
        expected=shown,
    )

    assert "delay: 2s" in devices.saved[0][1]


@pytest.mark.parametrize(
    ("location", "expected", "fragment"),
    [
        (_LOCATION, "on_boot:\n  then:\n    - delay: 9s\n", "differs from the expected text"),
        (
            {"kind": "device_on", "trigger": "on_shutdown"},
            "on_shutdown: {}\n",
            "no automation at that location any more",
        ),
    ],
    ids=["changed", "gone"],
)
async def test_upsert_with_expected_refuses_a_changed_or_missing_automation(
    tmp_path: Path, location: dict[str, Any], expected: str, fragment: str
) -> None:
    devices = _Devices()
    controller = _make_controller(tmp_path, devices=devices)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_REPLACEMENT,
            location=location,
            save=True,
            expected=expected,
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert fragment in excinfo.value.message
    assert devices.saved == []


async def test_upsert_with_expected_guards_a_draft_computation_too(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, devices=_Devices())

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_REPLACEMENT,
            location=_LOCATION,
            yaml=_YAML,
            expected="on_boot:\n  then:\n    - delay: 9s\n",
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED


async def test_upsert_refuses_save_beside_yaml(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, devices=_Devices())

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_AUTOMATION,
            location=_LOCATION,
            yaml=_YAML,
            save=True,
        )

    assert excinfo.value.code is ErrorCode.INVALID_ARGS


@pytest.mark.parametrize(
    "automation",
    [
        {"trigger_id": "on_boot", "actions": "delay"},
        {"trigger_id": "on_boot", "actions": [{"params": {}}]},
        "not even a mapping",
    ],
    ids=["actions_not_a_list", "node_without_action_id", "not_a_mapping"],
)
async def test_upsert_refuses_a_malformed_tree_as_invalid_args(
    tmp_path: Path, automation: dict[str, Any]
) -> None:
    controller = _make_controller(tmp_path, devices=_Devices())

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(configuration="d.yaml", automation=automation, location=_LOCATION)

    assert excinfo.value.code is ErrorCode.INVALID_ARGS
    assert excinfo.value.message.startswith("Invalid automation: ")
