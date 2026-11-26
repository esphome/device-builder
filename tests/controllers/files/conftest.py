"""Fixtures for the files controller tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from esphome_device_builder.controllers._device_scanner import DeviceScanner, ScanChange
from esphome_device_builder.controllers.devices import DevicesController, file_change
from esphome_device_builder.controllers.files import FilesController
from esphome_device_builder.models import Device

from ...conftest import MakeSettingsFactory
from ..devices.conftest import MakeControllerFactory, make_controller

__all__ = ["make_controller"]

# The loader decodes both to ``base.yaml``, but the source isn't that text verbatim.
UNREWRITABLE_INCLUDES = ['b: !include "\\u0062ase.yaml"\n', "b: !include >-\n  base.yaml\n"]


def write_tree(root: Path, files: dict[str, str]) -> None:
    """Write *files* (config-relative path to text) under *root*."""
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))  # no CRLF on Windows


@pytest.fixture
def setup(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
    make_settings: MakeSettingsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FilesController, DevicesController, list[tuple[str, str]]]:
    """Build a FilesController over a real DevicesController and real path validation."""
    devices = make_controller(tmp_path)
    devices._db.settings = make_settings()
    commits: list[tuple[str, str]] = []

    async def _commit(
        _: DevicesController, configurations: list[str], message: str, **__: object
    ) -> None:
        if message.startswith("Before: "):
            return  # the pre-change snapshot; test_rename covers it against real git
        commits.extend((configuration, message) for configuration in configurations)

    monkeypatch.setattr(file_change, "_commit_history_many", _commit)
    files = FilesController.__new__(FilesController)
    files._db = SimpleNamespace(settings=devices._db.settings, devices=devices)  # type: ignore[assignment]
    return files, devices, commits


Setup = tuple[FilesController, DevicesController, list[tuple[str, str]]]
Changes = list[tuple[ScanChange, str]]

DESK = "devices/lights/desk.yaml"
DOOR = "devices/garage/door.yaml"
PLUG_LAMP = "devices/plugs/lamp.yaml"
SWITCH_LAMP = "devices/switches/lamp.yaml"

TREE = {
    "kitchen.yaml": ("esphome:\n  name: kitchen\npackages:\n  base: !include common/base.yaml\n"),
    DESK: (
        "esphome:\n  name: desk\n  friendly_name: Desk\n"
        "packages:\n  base: !include ../../common/base.yaml\n"
    ),
    # No esphome.name: the fallbacks use the file's own stem, not its folder.
    DOOR: "logger:\n",
    "devices/garage/secrets.yaml": "garage_code: '1234'\n",
    PLUG_LAMP: "esphome:\n  name: plug-lamp\n",
    SWITCH_LAMP: "esphome:\n  name: switch-lamp\n",
    "secrets.yaml": "wifi_ssid: home\n",
    "secrets.ci.yaml": "wifi_ssid: ci\n",
    "common/base.yaml": "logger:\n  level: DEBUG\n",
    "common/secrets.yaml": "x: 1\n",
    "archive/old.yaml": "esphome:\n  name: old\n",
}


@pytest.fixture
def repo(tmp_path: Path, setup: Setup) -> tuple[Setup, DeviceScanner, Changes]:
    """Write ``TREE`` and swap the devices controller's scanner for a real one."""
    write_tree(tmp_path, TREE)
    _, devices, _ = setup
    devices._db.boards = None
    changes: Changes = []

    def _on_change(kind: ScanChange, device: Device, _previous: Device | None) -> None:
        changes.append((kind, device.configuration))

    scanner = DeviceScanner(
        tmp_path, devices._make_metadata_resolver, _on_change, devices._derive_board_id
    )
    devices._scanner = scanner
    return setup, scanner, changes
