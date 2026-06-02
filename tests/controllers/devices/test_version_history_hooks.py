"""Mutation sites record a rich-message version-history commit."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from .conftest import MakeControllerFactory


async def test_update_config_commits_edit_message(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """``devices/update_config`` records an "Edit <file>" commit."""
    controller = make_controller(tmp_path)
    record = AsyncMock(return_value="sha")
    controller._db.version_history = type("VH", (), {"record_configuration": record})()

    await controller.update_config(configuration="kitchen.yaml", content="esphome:\n  name: k\n")

    record.assert_awaited_once_with("kitchen.yaml", "Edit kitchen.yaml")


async def test_disabled_version_history_is_a_noop(
    make_controller: MakeControllerFactory, tmp_path: Path
) -> None:
    """With version_history None the write still succeeds (history is optional)."""
    controller = make_controller(tmp_path)
    assert controller._db.version_history is None

    await controller.update_config(configuration="kitchen.yaml", content="esphome:\n  name: k\n")

    assert (tmp_path / "kitchen.yaml").read_text() == "esphome:\n  name: k\n"
