"""Tests for ``devices/create`` on a board that ships a ``demo.yaml`` overlay."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from ruamel.yaml import YAML

from esphome_device_builder.controllers.components import _load_body_from_disk
from esphome_device_builder.controllers.devices import mutations_yaml
from esphome_device_builder.definitions import _BOARDS_DIR as BOARDS_DIR
from esphome_device_builder.definitions import BOARD_DEMO_FILENAME
from esphome_device_builder.helpers.yaml import merge_yaml_overlay
from esphome_device_builder.models import BoardCatalogEntry, BoardEsphomeConfig, Platform

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


@pytest.mark.xdist_group("catalog")
async def test_xiao_147_display_keeps_its_mirror_transform(session_component_catalog: Any) -> None:
    """The display preset's ``transform`` mapping reaches the created YAML."""
    board = await session_component_catalog._db.boards.get_board(
        board_id="seeed-xiao-nrf52840-147-touch-display"
    )
    assert board is not None
    text, _ = await mutations_yaml.yaml_content_for_create(
        "dev", "Dev", board, None, "", "", catalog=session_component_catalog
    )
    (display,) = YAML(typ="rt").load(text)["display"]
    assert dict(display["transform"]) == {"swap_xy": False, "mirror_x": True, "mirror_y": False}


def test_mipi_transform_is_a_yaml_only_union() -> None:
    """``transform`` (``disabled`` or a mirror mapping) is a YAML-only ``unknown`` union."""
    body = _load_body_from_disk("display.mipi_spi")
    assert body is not None
    (transform,) = (e for e in body.config_entries if e.key == "transform")
    assert transform.type == "unknown"
    assert not transform.options
