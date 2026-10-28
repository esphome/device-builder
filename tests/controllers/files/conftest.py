"""Fixtures for the files controller tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from esphome_device_builder.controllers.devices import DevicesController, file_change
from esphome_device_builder.controllers.files import FilesController

from ...conftest import MakeSettingsFactory
from ..devices.conftest import MakeControllerFactory, make_controller

__all__ = ["make_controller"]


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

    async def _commit(_: DevicesController, configurations: list[str], message: str) -> None:
        if message.startswith("Before: "):
            return  # the pre-change snapshot; test_rename covers it against real git
        commits.extend((configuration, message) for configuration in configurations)

    monkeypatch.setattr(file_change, "_commit_history_many", _commit)
    files = FilesController.__new__(FilesController)
    files._db = SimpleNamespace(settings=devices._db.settings, devices=devices)  # type: ignore[assignment]
    return files, devices, commits
