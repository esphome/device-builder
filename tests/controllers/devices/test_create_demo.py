"""Tests for ``devices/create`` on a board that ships a ``demo.yaml`` overlay."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from ruamel.yaml import YAML

from esphome_device_builder.controllers.components import _load_body_from_disk
from esphome_device_builder.definitions import _BOARDS_DIR as BOARDS_DIR
from esphome_device_builder.definitions import BOARD_DEMO_FILENAME, load_board_body_from_disk
from esphome_device_builder.helpers.device_yaml import generate_device_yaml
from esphome_device_builder.helpers.yaml import merge_yaml_overlay
from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardEsphomeConfig,
    ComponentCatalogEntry,
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


def test_shipped_demos_merge_onto_their_board() -> None:
    """Every shipped demo merges onto its board's template and defaults without duplicate keys."""
    demos = sorted(BOARDS_DIR.glob(f"*/{BOARD_DEMO_FILENAME}"))
    assert demos
    for demo in demos:
        board = load_board_body_from_disk(demo.parent.name)
        assert board is not None, demo
        assert board.demo_yaml == demo.read_text(encoding="utf-8"), demo
        text = generate_device_yaml(
            "dev", "Dev", board, "", "", wifi_secrets_available=False, defaults=_defaults(board)
        )
        YAML(typ="rt").load(merge_yaml_overlay(text, board.demo_yaml))


def _defaults(board: BoardCatalogEntry) -> list[tuple[ComponentCatalogEntry, dict[str, Any]]]:
    """Resolve *board*'s default components from disk, featured presets plus overrides."""
    featured = {fc.id: fc for fc in board.featured_components}
    out: list[tuple[ComponentCatalogEntry, dict[str, Any]]] = []
    for entry in board.default_components:
        fc = featured.get(entry.id)
        body = _load_body_from_disk(fc.component_id if fc is not None else entry.id)
        assert body is not None, entry.id
        presets = fc.fields.items() if fc is not None else ()
        fields = {k: p.value for k, p in presets if p.value is not None}
        out.append((body, {**fields, **entry.fields}))
    return out
