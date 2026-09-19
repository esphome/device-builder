"""Tests for the ``save`` option of ``automations/delete`` and the one-job config reads."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from esphome_device_builder.controllers.automations import AutomationsController
from esphome_device_builder.controllers.automations import controller as automations_controller
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode
from esphome_device_builder.models.automations import YamlDiff

pytestmark = pytest.mark.xdist_group("automations")

_YAML = "esphome:\n  name: d\n  on_boot:\n    then:\n      - delay: 1s\n"
_LOCATION = {"kind": "device_on", "trigger": "on_boot"}


class _Devices:
    """Stand-in for the devices controller's locked read-rewrite-save."""

    def __init__(self) -> None:
        self.saved: list[tuple[str, str, str]] = []

    async def rewrite_yaml(
        self, configuration: str, rewrite: Callable[[str], tuple[str, YamlDiff]], *, message: str
    ) -> YamlDiff:
        new_text, diff = rewrite(_YAML)
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
