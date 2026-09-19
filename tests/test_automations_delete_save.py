"""Tests for the ``save`` option of ``automations/delete``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.automations import AutomationsController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode

pytestmark = pytest.mark.xdist_group("automations")

_YAML = "esphome:\n  name: d\n  on_boot:\n    then:\n      - delay: 1s\n"
_LOCATION = {"kind": "device_on", "trigger": "on_boot"}


def _make_controller(config_dir: Path, *, devices: Any) -> AutomationsController:
    (config_dir / "d.yaml").write_text(_YAML, encoding="utf-8")
    db = MagicMock()
    db.settings.rel_path = config_dir.joinpath
    db.devices = devices
    return AutomationsController(db)


async def test_delete_with_save_writes_the_spliced_config(tmp_path: Path) -> None:
    devices = SimpleNamespace(apply_automation_edit=AsyncMock())
    controller = _make_controller(tmp_path, devices=devices)

    result = await controller.delete(configuration="d.yaml", location=_LOCATION, save=True)

    assert result["yaml_diff"]["replacement"] == ""
    devices.apply_automation_edit.assert_awaited_once_with("d.yaml", "esphome:\n  name: d\n")


async def test_delete_without_save_leaves_the_config_alone(tmp_path: Path) -> None:
    devices = SimpleNamespace(apply_automation_edit=AsyncMock())
    controller = _make_controller(tmp_path, devices=devices)

    await controller.delete(configuration="d.yaml", location=_LOCATION)

    devices.apply_automation_edit.assert_not_awaited()


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"save": True, "yaml": _YAML}, id="save_with_a_draft"),
        pytest.param({"save": "yes"}, id="non_boolean_save"),
    ],
)
async def test_delete_refuses_invalid_save_args(tmp_path: Path, args: dict[str, Any]) -> None:
    devices = SimpleNamespace(apply_automation_edit=AsyncMock())
    controller = _make_controller(tmp_path, devices=devices)

    with pytest.raises(CommandError) as err:
        await controller.delete(configuration="d.yaml", location=_LOCATION, **args)

    assert err.value.code == ErrorCode.INVALID_ARGS
    devices.apply_automation_edit.assert_not_awaited()


async def test_delete_with_save_needs_the_devices_controller(tmp_path: Path) -> None:
    controller = _make_controller(tmp_path, devices=None)

    with pytest.raises(CommandError) as err:
        await controller.delete(configuration="d.yaml", location=_LOCATION, save=True)

    assert err.value.code == ErrorCode.UNAVAILABLE
