"""Tests for ``devices/create`` on a board that ships a ``demo.yaml``."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from ruamel.yaml import YAML

from esphome_device_builder.definitions import _BOARDS_DIR as BOARDS_DIR
from esphome_device_builder.definitions import BOARD_DEMO_FILENAME
from esphome_device_builder.helpers.device_yaml import generate_demo_yaml
from esphome_device_builder.models import BoardCatalogEntry, BoardEsphomeConfig, Platform

from .conftest import MakeControllerFactory

_DEMO = (
    "esphome:\n  name: ${name}\n  friendly_name: ${friendly_name}\n\nnrf52:\n  board: xiao_ble\n"
)


def _demo_board() -> BoardCatalogEntry:
    """Build an nRF52 catalog entry carrying a demo."""
    return BoardCatalogEntry(
        id="demo-board",
        name="Demo Board",
        description="",
        manufacturer="Acme",
        esphome=BoardEsphomeConfig(platform=Platform.NRF52, board="xiao_ble"),
        demo_yaml=_DEMO,
    )


def test_generate_demo_yaml_binds_name_substitutions() -> None:
    """The demo lands verbatim under a ``substitutions:`` block naming the device."""
    text = generate_demo_yaml("desk-level", "Desk #2", _demo_board())

    parsed = YAML(typ="safe").load(text)
    assert parsed["substitutions"] == {"name": "desk-level", "friendly_name": "Desk #2"}
    assert text.endswith(_DEMO)
    assert "definitions/boards/demo-board/manifest.yaml" in text


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_writes_validated_demo(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A demo board create writes the demo and validates it strictly."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    ctrl._db.boards.get_board = AsyncMock(return_value=_demo_board())

    await ctrl.create_device(name="level", board_id="demo-board")

    content = (tmp_path / "level.yaml").read_text(encoding="utf-8")
    assert content.endswith(_DEMO)
    assert "  name: level\n" in content
    ctrl._db.editor.validate_yaml.assert_awaited_once()


def test_shipped_demos_use_name_substitutions() -> None:
    """Every shipped demo names the device through the create-time substitutions."""
    demos = sorted(BOARDS_DIR.glob(f"*/{BOARD_DEMO_FILENAME}"))
    assert demos
    for demo in demos:
        text = demo.read_text(encoding="utf-8")
        assert "  name: ${name}\n" in text, demo
        assert "  friendly_name: ${friendly_name}\n" in text, demo
        assert not text.startswith("substitutions:"), demo
        assert "\nsubstitutions:" not in text, demo
