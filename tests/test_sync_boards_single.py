"""
Pin the single-board sync path and the ESPHome-version guard.

The single-board path reuses the full sync's index/featured writers but its
own body writer, so these pin that it stays byte-identical to a full sync,
touches only the named body, and refuses a mismatched ESPHome.
"""

from __future__ import annotations

import esphome.const
import orjson
import pytest

import script.sync_boards as sb
from esphome_device_builder.models import BoardCatalogResponse

# Reuse the session-scoped catalog (one ESPHome import + generation per worker).
pytestmark = pytest.mark.xdist_group("board_sync")


def _redirect_outputs(monkeypatch, root):
    monkeypatch.setattr(sb, "_BODIES_DIR", root / "board_bodies")
    monkeypatch.setattr(sb, "_INDEX_FILE", root / "boards.index.json")
    monkeypatch.setattr(sb, "_FEATURED_INDEX_FILE", root / "featured_components.index.json")


def test_single_board_emit_matches_full_index_and_featured(
    generated_board_catalog: BoardCatalogResponse, monkeypatch, tmp_path
):
    boards = generated_board_catalog.boards
    full_payloads = [board.to_dict() for board in boards]
    board_id = boards[0].id
    _redirect_outputs(monkeypatch, tmp_path)

    sb._emit_split_catalog(boards, full_payloads)
    sb._emit_featured_components_index(boards)
    index_full = sb._INDEX_FILE.read_bytes()
    featured_full = sb._FEATURED_INDEX_FILE.read_bytes()

    sb._emit_single_board(boards, full_payloads, board_id)

    assert sb._INDEX_FILE.read_bytes() == index_full
    assert sb._FEATURED_INDEX_FILE.read_bytes() == featured_full


def test_single_board_writes_only_the_target_body(
    generated_board_catalog: BoardCatalogResponse, monkeypatch, tmp_path
):
    boards = generated_board_catalog.boards
    full_payloads = [board.to_dict() for board in boards]
    board_id = boards[0].id

    # Reference: the body a full sync writes for this board.
    _redirect_outputs(monkeypatch, tmp_path / "full")
    sb._emit_split_catalog(boards, full_payloads)
    body_full = (sb._BODIES_DIR / f"{board_id}.json").read_bytes()

    # Single-board mode into an empty bodies dir.
    _redirect_outputs(monkeypatch, tmp_path / "single")
    sb._BODIES_DIR.mkdir(parents=True)
    sb._emit_single_board(boards, full_payloads, board_id)

    assert [p.name for p in sb._BODIES_DIR.iterdir()] == [f"{board_id}.json"]
    assert (sb._BODIES_DIR / f"{board_id}.json").read_bytes() == body_full


def test_emit_single_board_unknown_id_raises(
    generated_board_catalog: BoardCatalogResponse, monkeypatch, tmp_path
):
    _redirect_outputs(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        sb._emit_single_board(generated_board_catalog.boards, [], "no_such_board")


def test_canonical_esphome_version():
    assert sb._canonical_esphome_version("2099.1.1b3") == "2099.1.1"
    assert sb._canonical_esphome_version("2099.1.1-dev") == "2099.1.1"
    assert sb._canonical_esphome_version("2099.1.1") == "2099.1.1"


def test_require_matching_esphome(monkeypatch, tmp_path):
    index = tmp_path / "boards.index.json"
    index.write_bytes(orjson.dumps({"esphome_version": "2099.1.1", "boards": []}))
    monkeypatch.setattr(sb, "_INDEX_FILE", index)

    monkeypatch.setattr(esphome.const, "__version__", "2099.1.1")
    sb._require_matching_esphome()  # exact match: no raise

    monkeypatch.setattr(esphome.const, "__version__", "2099.1.1b3")
    sb._require_matching_esphome()  # beta canonicalizes to the base: no raise

    monkeypatch.setattr(esphome.const, "__version__", "2000.0.0")
    with pytest.raises(SystemExit, match=r"2099\.1\.1"):
        sb._require_matching_esphome()
