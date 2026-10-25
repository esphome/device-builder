"""Tests for ``devices/create`` on a board that ships a ``demo.yaml`` overlay."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from ruamel.yaml import YAML

from esphome_device_builder.controllers.devices import mutations_yaml
from esphome_device_builder.definitions import _BOARDS_DIR as BOARDS_DIR
from esphome_device_builder.definitions import BOARD_DEMO_FILENAME
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.yaml import merge_yaml_overlay
from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardEsphomeConfig,
    ErrorCode,
    Platform,
)

from .conftest import MakeControllerFactory

_BASE = (
    "esphome:\n  name: dev\n\nnrf52:\n  board: xiao_ble\n\nswitch:\n  - platform: gpio\n    id: a\n"
)
_DEMO = (
    "nrf52:\n  dfu: true\n\n"
    "# Second LED\n"
    "switch:\n  - platform: gpio\n    id: b\n\n"
    "# Status page\n"
    "lvgl:\n  pages: []\n"
)


def _demo_board() -> BoardCatalogEntry:
    """Build an nRF52 catalog entry carrying a demo overlay."""
    return BoardCatalogEntry(
        id="demo-board",
        name="Demo Board",
        description="",
        manufacturer="Acme",
        esphome=BoardEsphomeConfig(platform=Platform.NRF52, board="xiao_ble"),
        demo_yaml=_DEMO,
    )


def test_merge_yaml_overlay_merges_appends_and_keeps_comments() -> None:
    """Mappings gain keys, lists gain items, absent blocks append with their comments."""
    merged = merge_yaml_overlay(_BASE, _DEMO)

    parsed = YAML(typ="safe").load(merged)
    assert parsed["nrf52"] == {"board": "xiao_ble", "dfu": True}
    assert [item["id"] for item in parsed["switch"]] == ["a", "b"]
    assert parsed["lvgl"] == {"pages": []}
    assert "  # Second LED\n" in merged
    assert "# Status page\nlvgl:" in merged


def test_merge_yaml_overlay_extends_item_by_id() -> None:
    """A ``- id: !extend <id>`` item adds its keys to that existing item, nowhere else."""
    overlay = "switch:\n  - id: !extend a\n    on_turn_on:\n      - logger.log: hi\n"
    merged = merge_yaml_overlay(_BASE, overlay)

    parsed = YAML(typ="safe").load(merged)
    assert parsed["switch"] == [
        {"platform": "gpio", "id": "a", "on_turn_on": [{"logger.log": "hi"}]}
    ]
    assert "!extend" not in merged


def test_merge_yaml_overlay_adds_keys_beside_a_nested_list() -> None:
    """Mapping keys land at the block's child indent, not a nested list's dash column."""
    base = "esphome:\n  name: dev\n  on_boot:\n    - logger.log: hi\n"
    merged = merge_yaml_overlay(base, "esphome:\n  min_version: 2026.1.0\n")

    assert YAML(typ="safe").load(merged)["esphome"] == {
        "name": "dev",
        "on_boot": [{"logger.log": "hi"}],
        "min_version": "2026.1.0",
    }


def test_merge_yaml_overlay_appends_a_bare_block() -> None:
    """A body-less block the base lacks (``captive_portal:``) is appended, not dropped."""
    merged = merge_yaml_overlay(_BASE, "captive_portal:\n")

    assert "captive_portal" in YAML(typ="safe").load(merged)


def test_merge_yaml_overlay_extend_line_may_carry_a_comment() -> None:
    """A trailing comment on the ``- id: !extend`` line still extends the item."""
    merged = merge_yaml_overlay(_BASE, "switch:\n  - id: !extend a  # note\n    x: 1\n")

    assert YAML(typ="safe").load(merged)["switch"] == [{"platform": "gpio", "id": "a", "x": 1}]


def test_merge_yaml_overlay_keeps_comments_of_extend_only_and_trailing_text() -> None:
    """Comments above an all-``!extend`` block and after the last block survive."""
    overlay = "# Blink a\nswitch:\n  - id: !extend a\n    x: 1\n\nlogger:\n# The end\n"
    merged = merge_yaml_overlay(_BASE, overlay)

    assert "    x: 1\n  # Blink a\n" in merged
    assert merged.endswith("logger:\n\n# The end\n")
    assert YAML(typ="safe").load(merged)["switch"] == [{"platform": "gpio", "id": "a", "x": 1}]


def test_merge_yaml_overlay_keeps_a_shallow_comment_commented() -> None:
    """A column-0 comment inside a body moves to the child indent instead of going live."""
    merged = merge_yaml_overlay(_BASE, "nrf52:\n  dfu: true\n\n# dfu_x: 1\n  y: 2\n")

    assert "  # dfu_x: 1\n" in merged
    assert YAML(typ="safe").load(merged)["nrf52"] == {"board": "xiao_ble", "dfu": True, "y": 2}


def test_merge_yaml_overlay_opens_a_block_at_any_column_0_key() -> None:
    """An underscore-led key at column 0 starts its own block, not a line of the previous one."""
    merged = merge_yaml_overlay(_BASE, "nrf52:\n  dfu: true\n_x:\n  a: 1\n")

    parsed = YAML(typ="safe").load(merged)
    assert parsed["nrf52"] == {"board": "xiao_ble", "dfu": True}
    assert parsed["_x"] == {"a": 1}


def test_merge_yaml_overlay_extends_last_item_without_trailing_newline() -> None:
    """Extending the file's final item works when the base lacks a trailing newline."""
    overlay = "switch:\n  - id: !extend a\n    restore_mode: ALWAYS_ON\n"
    merged = merge_yaml_overlay(_BASE.rstrip("\n"), overlay)

    assert YAML(typ="safe").load(merged)["switch"] == [
        {"platform": "gpio", "id": "a", "restore_mode": "ALWAYS_ON"}
    ]


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        pytest.param("nrf52:\n  board: other\n", "repeats board", id="repeated_key"),
        pytest.param("nrf52:\n  - board: other\n", "list and mapping", id="shape_mismatch"),
        pytest.param(
            "switch:\n  - id: !extend a\n    platform: x\n", "repeats platform", id="extend_repeat"
        ),
        pytest.param("switch:\n  - id: !extend z\n    x: 1\n", "matches no item", id="extend_miss"),
        pytest.param("light:\n  - id: !extend a\n    x: 1\n", "no list", id="extend_no_block"),
        pytest.param(
            "switch:\n  - platform: gpio\n    id: !extend a\n", "write !extend", id="extend_form"
        ),
        pytest.param("logger: {}\n", "must be a block", id="inline_value"),
        pytest.param("---\nlogger:\n", "precede the first block", id="leading_text"),
        pytest.param("nrf52:\n# note\n  board: other\n", "repeats board", id="comment_first"),
        pytest.param("nrf52:\n    dfu: true\n  y: 2\n", "indented less", id="under_indent"),
        pytest.param(
            "light:\n  - platform: x\nlight:\n  - platform: y\n", "repeated", id="repeated_block"
        ),
    ],
)
def test_merge_yaml_overlay_refuses_conflicts(overlay: str, message: str) -> None:
    """An overlay contradicting the base raises instead of emitting a broken config."""
    with pytest.raises(ValueError, match=message):
        merge_yaml_overlay(_BASE, overlay)


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_merges_demo_into_template(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A demo board create writes the template with the demo merged in, validated strictly."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    ctrl._db.boards.get_board = AsyncMock(return_value=_demo_board())

    await ctrl.create_device(name="level", board_id="demo-board")

    parsed = YAML(typ="safe").load((tmp_path / "level.yaml").read_text(encoding="utf-8"))
    assert parsed["esphome"]["name"] == "level"
    assert parsed["nrf52"] == {"board": "xiao_ble", "dfu": True}
    assert "lvgl" in parsed
    ctrl._db.editor.validate_yaml.assert_awaited_once()


@pytest.mark.usefixtures("stub_create_device_metadata_helpers")
async def test_create_device_refuses_a_conflicting_demo(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A demo that can't merge onto the template is a generator ``INTERNAL_ERROR``."""
    ctrl = make_controller(tmp_path, with_state_monitor=True, with_boards=True)
    board = _demo_board()
    board.demo_yaml = "nrf52:\n  board: other\n"
    ctrl._db.boards.get_board = AsyncMock(return_value=board)

    with pytest.raises(CommandError, match="demo-board demo config") as excinfo:
        await ctrl.create_device(name="level", board_id="demo-board")
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR
    assert not (tmp_path / "level.yaml").exists()


@pytest.mark.xdist_group("catalog")
async def test_shipped_demos_merge_through_create(session_component_catalog: Any) -> None:
    """Every shipped demo merges onto its board's real defaults in the create path."""
    demos = sorted(BOARDS_DIR.glob(f"*/{BOARD_DEMO_FILENAME}"))
    assert demos
    for demo in demos:
        board = await session_component_catalog._db.boards.get_board(board_id=demo.parent.name)
        assert board is not None, demo
        assert board.demo_yaml == demo.read_text(encoding="utf-8"), demo
        text, source = await mutations_yaml.yaml_content_for_create(
            "dev", "Dev", board, None, "", "", catalog=session_component_catalog
        )
        assert source == "template"
        assert "!extend" not in text, demo
        YAML(typ="rt").load(text)
