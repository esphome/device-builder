"""Pin the single-board sync path and the ESPHome-version guard in sync_boards.

The single-board path (``sync_boards.py <id>``) reuses the full path's index
and featured-map writers but its own body writer, so these pin that it stays
byte-identical to a full sync and touches only the named body, and that the
version guard refuses a mismatched ESPHome.
"""

from __future__ import annotations

import esphome.const
import orjson
import pytest

import script.sync_boards as sb


@pytest.fixture(scope="module")
def catalog():
    return sb.build_catalog()


def _redirect_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "_BODIES_DIR", tmp_path / "board_bodies")
    monkeypatch.setattr(sb, "_INDEX_FILE", tmp_path / "boards.index.json")
    monkeypatch.setattr(sb, "_FEATURED_INDEX_FILE", tmp_path / "featured_components.index.json")


def test_single_board_emit_matches_full_index_and_featured(catalog, monkeypatch, tmp_path):
    boards = catalog.boards
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


def test_single_board_writes_only_the_target_body(catalog, monkeypatch, tmp_path):
    boards = catalog.boards
    full_payloads = [board.to_dict() for board in boards]
    board_id = boards[0].id
    _redirect_outputs(monkeypatch, tmp_path)
    sb._BODIES_DIR.mkdir()

    sb._emit_single_board(boards, full_payloads, board_id)

    assert [p.name for p in sb._BODIES_DIR.iterdir()] == [f"{board_id}.json"]


def test_emit_single_board_unknown_id_raises(catalog, monkeypatch, tmp_path):
    _redirect_outputs(monkeypatch, tmp_path)
    with pytest.raises(SystemExit):
        sb._emit_single_board(catalog.boards, [], "no_such_board")


def test_require_matching_esphome(monkeypatch, tmp_path):
    index = tmp_path / "components.index.json"
    index.write_bytes(orjson.dumps({"esphome_schema_version": "2099.1.1"}))
    monkeypatch.setattr(sb, "_COMPONENTS_INDEX_FILE", index)

    monkeypatch.setattr(esphome.const, "__version__", "2099.1.1")
    sb._require_matching_esphome()  # match: no raise

    monkeypatch.setattr(esphome.const, "__version__", "2000.0.0")
    with pytest.raises(SystemExit, match=r"2099\.1\.1"):
        sb._require_matching_esphome()
