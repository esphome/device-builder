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


def _setup(config_dir: Path, text: str = _YAML) -> tuple[AutomationsController, _Devices]:
    devices = _Devices(text)
    return _make_controller(config_dir, devices=devices), devices


@pytest.mark.parametrize("save", [True, False])
async def test_delete_writes_the_spliced_config_only_with_save(tmp_path: Path, save: bool) -> None:
    controller, devices = _setup(tmp_path)

    result = await controller.delete(configuration="d.yaml", location=_LOCATION, save=save)

    assert result["yaml_diff"] == {"fromLine": 3, "toLine": 5, "replacement": ""}
    saved = [("d.yaml", "esphome:\n  name: d\n", "Delete an automation from d.yaml")]
    assert devices.saved == (saved if save else [])


async def test_delete_with_expected_removes_only_the_automation_it_was_shown(
    tmp_path: Path,
) -> None:
    controller, devices = _setup(tmp_path)
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
    controller, devices = _setup(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await controller.delete(
            configuration="d.yaml", location=location, save=True, expected=expected
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert fragment in excinfo.value.message
    if fragment.startswith("differs"):
        assert "nothing was written, list again and retry" in excinfo.value.message
        assert "-    - delay: 2s\n+    - delay: 1s\n" in excinfo.value.message
    assert devices.saved == []


async def test_delete_with_expected_refuses_a_positional_index_that_shifted(
    tmp_path: Path,
) -> None:
    item = "  - interval: {n}s\n    then:\n      - delay: {n}s\n"
    listed = "interval:\n" + item.format(n=1)
    on_disk = "interval:\n" + item.format(n=9) + item.format(n=1)
    controller, devices = _setup(tmp_path, on_disk)
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
    controller, devices = _setup(tmp_path, "esphome: [\n")

    with pytest.raises(CommandError) as excinfo:
        await controller.delete(
            configuration="d.yaml", location=_LOCATION, save=True, expected="on_boot: {}\n"
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert excinfo.value.message.startswith("the config no longer loads, nothing was written: ")
    assert isinstance(excinfo.value.__cause__, CommandError)
    assert devices.saved == []


async def test_delete_with_expected_passes_other_parser_errors_through(tmp_path: Path) -> None:
    controller, devices = _setup(tmp_path)
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
    controller, devices = _setup(tmp_path)

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


_LIST_SHAPED = (
    "esphome:\n  name: d\n  on_boot:\n"
    "    - then:\n        - delay: 9s\n    - then:\n        - delay: 8s\n"
)
_INTERVAL = "interval:\n  - interval: 1s\n    then:\n      - delay: 1s\n"
_SHORTHAND = (
    "button:\n  - platform: template\n    name: B\n    id: bid\n"
    "    on_press:\n      - switch.turn_off: relay\n"
)
_TIMED = _AUTOMATION | {"trigger_id": None, "trigger_params": {"interval": "5s"}}


async def test_upsert_with_save_inserts_at_an_empty_location(tmp_path: Path) -> None:
    controller, devices = _setup(tmp_path)

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


@pytest.mark.parametrize(
    ("text", "automation", "location", "needle", "count", "also_contains"),
    [
        pytest.param(
            _LIST_SHAPED,
            _AUTOMATION,
            {"kind": "device_on", "trigger": "on_boot", "index": 2},
            "- then:",
            3,
            None,
            id="list_shaped_handler",
        ),
        pytest.param(
            _SHORTHAND,
            _AUTOMATION | {"trigger_id": None},
            {"kind": "component_on", "component_id": "bid", "trigger": "on_press", "index": 1},
            "- then:",
            2,
            "switch.turn_off: relay",
            id="bare_action_list_shorthand",
        ),
        pytest.param(
            _INTERVAL,
            _TIMED,
            {"kind": "interval", "index": 1},
            "- interval:",
            2,
            None,
            id="interval",
        ),
        pytest.param(
            "api:\n  services:\n    - service: ping\n      then:\n        - delay: 1s\n",
            _AUTOMATION | {"trigger_id": None},
            {"kind": "api_action", "action_name": "pong"},
            "- action:",
            2,
            None,
            id="legacy_api_services",
        ),
    ],
)
async def test_upsert_with_save_appends_beside_existing_automations(
    tmp_path: Path,
    text: str,
    automation: dict[str, Any],
    location: dict[str, Any],
    needle: str,
    count: int,
    also_contains: str | None,
) -> None:
    controller, devices = _setup(tmp_path, text)

    await controller.upsert(
        configuration="d.yaml", automation=automation, location=location, save=True
    )

    assert devices.saved[0][1].count(needle) == count
    assert also_contains is None or also_contains in devices.saved[0][1]


_WITH_INCLUDE = _INTERVAL + "  - !include more.yaml\n"
_MAPPED = "interval:\n  interval: 1s\n  then:\n    - delay: 1s\n"


async def test_upsert_with_save_appends_beside_a_mapping_form_interval(tmp_path: Path) -> None:
    controller, devices = _setup(tmp_path, _MAPPED)

    await controller.upsert(
        configuration="d.yaml",
        automation=_TIMED,
        location={"kind": "interval", "index": 1},
        save=True,
    )

    assert devices.saved[0][1].count("- interval:") == 2


async def test_upsert_with_expected_replaces_a_mapping_form_interval(tmp_path: Path) -> None:
    controller, devices = _setup(tmp_path, _MAPPED)
    shown = (await asyncio.to_thread(parsing.parse_device_yaml, _MAPPED))[0]

    await controller.upsert(
        configuration="d.yaml",
        automation=_TIMED,
        location=shown.location.to_dict(),
        save=True,
        expected=shown.raw_yaml,
    )

    assert devices.saved[0][1].count("- interval:") == 1
    assert "interval: 5s" in devices.saved[0][1]


async def test_upsert_with_save_appends_after_an_entry_the_parser_skips(tmp_path: Path) -> None:
    controller, devices = _setup(tmp_path, _WITH_INCLUDE)

    await controller.upsert(
        configuration="d.yaml",
        automation=_TIMED,
        location={"kind": "interval", "index": 2},
        save=True,
    )

    saved = devices.saved[0][1]
    assert saved.count("- interval:") == 2 and "!include more.yaml" in saved


async def test_upsert_with_save_refuses_an_index_past_the_end(tmp_path: Path) -> None:
    controller, devices = _setup(tmp_path, _INTERVAL)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_TIMED,
            location={"kind": "interval", "index": 5},
            save=True,
        )

    assert excinfo.value.code is ErrorCode.INVALID_ARGS
    assert excinfo.value.message == "interval[5] out of range (have 1)"
    assert devices.saved == []


async def test_upsert_with_save_reports_a_rewrite_that_no_longer_loads_as_its_own_fault(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    controller, devices = _setup(tmp_path)

    with (
        patch.object(
            automations_controller.writing,
            "render_upsert",
            return_value=("esphome: [\n", YamlDiff(fromLine=1, toLine=1, replacement="")),
        ),
        pytest.raises(CommandError) as excinfo,
    ):
        await controller.upsert(
            configuration="d.yaml",
            automation=_AUTOMATION,
            location={"kind": "device_on", "trigger": "on_shutdown"},
            save=True,
        )

    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
    assert excinfo.value.message.startswith("the rewrite produced a config that does not load")
    assert "Automation rewrite produced a config that does not load" in caplog.text
    assert devices.saved == []


async def test_upsert_with_save_keeps_working_beside_a_nan_survivor(tmp_path: Path) -> None:
    controller, devices = _setup(tmp_path, _INTERVAL.replace("delay: 1s", "delay: .nan"))

    await controller.upsert(
        configuration="d.yaml",
        automation=_TIMED,
        location={"kind": "interval", "index": 1},
        save=True,
    )

    assert devices.saved[0][1].count("- interval:") == 2


async def test_upsert_with_save_passes_other_parser_errors_through(tmp_path: Path) -> None:
    controller, devices = _setup(tmp_path)
    other = CommandError(ErrorCode.UNAVAILABLE, "catalog not loaded")
    before = await asyncio.to_thread(automations_controller.parsing.parse_device_yaml, _YAML)

    with (
        patch.object(
            automations_controller.parsing, "parse_device_yaml", side_effect=[before, other]
        ),
        pytest.raises(CommandError) as excinfo,
    ):
        await controller.upsert(
            configuration="d.yaml",
            automation=_AUTOMATION,
            location={"kind": "device_on", "trigger": "on_shutdown"},
            save=True,
        )

    assert excinfo.value is other
    assert devices.saved == []


async def test_upsert_with_save_refuses_to_overwrite_an_entry_the_parser_skips(
    tmp_path: Path,
) -> None:
    controller, devices = _setup(tmp_path, _WITH_INCLUDE)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml",
            automation=_TIMED,
            location={"kind": "interval", "index": 1},
            save=True,
        )

    assert excinfo.value.code is ErrorCode.INVALID_ARGS
    assert (
        excinfo.value.message == "interval[1] is not an entry the parser lists; append at 2 instead"
    )
    assert devices.saved == []


@pytest.mark.parametrize(
    ("text", "automation", "location", "fragment"),
    [
        pytest.param(_YAML, _REPLACEMENT, _LOCATION, "already holds YAML", id="occupied"),
        pytest.param(
            _LIST_SHAPED, _REPLACEMENT, _LOCATION, "already holds YAML", id="list_shaped_handler"
        ),
        pytest.param(
            "esphome: [\n",
            _AUTOMATION,
            {"kind": "device_on", "trigger": "on_shutdown"},
            "no longer loads",
            id="no_longer_loads",
        ),
    ],
)
async def test_upsert_with_save_refuses_an_insert_that_is_not_clean(
    tmp_path: Path,
    text: str,
    automation: dict[str, Any],
    location: dict[str, Any],
    fragment: str,
) -> None:
    controller, devices = _setup(tmp_path, text)

    with pytest.raises(CommandError) as excinfo:
        await controller.upsert(
            configuration="d.yaml", automation=automation, location=location, save=True
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert fragment in excinfo.value.message
    assert devices.saved == []


async def test_upsert_with_expected_refuses_when_the_writer_would_append_instead(
    tmp_path: Path,
) -> None:
    idless = "script:\n  - then:\n      - delay: 1s\n"
    controller, devices = _setup(tmp_path, idless)
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
    controller, devices = _setup(tmp_path, "esphome: [\n")

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
    controller, devices = _setup(tmp_path)
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
    controller, devices = _setup(tmp_path)

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
