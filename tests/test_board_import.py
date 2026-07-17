"""Unit tests for the shared board-import helpers in ``script/_board_import.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from script._board_import import (
    emit_manifest,
    hash_content,
    imported_remote_id,
    prune_removed,
)

_RECORD = {
    "id": "some_board",
    "name": "Some Board",
    "esphome": {"platform": "esp32", "board": "esp32dev"},
    "source": {"type": "source-a", "remote_id": "Some-Board"},
}


def _write_manifest(boards_dir: Path, board_id: str, source_type: str | None) -> Path:
    board_dir = boards_dir / board_id
    board_dir.mkdir(parents=True)
    source = f"source:\n  type: {source_type}\n  remote_id: {board_id}\n" if source_type else ""
    path = board_dir / "manifest.yaml"
    path.write_text(f"id: {board_id}\nname: X\n{source}", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("manifest_type", "query_type", "expected"),
    [
        pytest.param("source-a", "source-a", (True, "Some-Board"), id="own_type"),
        pytest.param("source-a", "source-b", (False, None), id="foreign_type"),
        pytest.param(None, "source-a", (False, None), id="curated"),
    ],
)
def test_imported_remote_id_is_per_source_type(
    manifest_type: str | None, query_type: str, expected: tuple[bool, str | None]
) -> None:
    prior: dict | None = {"id": "x"}
    if manifest_type is not None:
        prior["source"] = {"type": manifest_type, "remote_id": "Some-Board"}
    assert imported_remote_id(prior, source_type=query_type) == expected


@pytest.mark.parametrize(
    ("existing_type", "emitted"),
    [
        pytest.param(None, False, id="curated_refused"),
        pytest.param("source-b", False, id="cross_source_refused"),
        pytest.param("source-a", True, id="own_type_overwritten"),
    ],
)
def test_emit_manifest_ownership(tmp_path: Path, existing_type: str | None, emitted: bool) -> None:
    _write_manifest(tmp_path, "some_board", existing_type)
    result = emit_manifest(dict(_RECORD), source_type="source-a", boards_dir=tmp_path)
    manifest = tmp_path / "some_board" / "manifest.yaml"
    if emitted:
        assert result == tmp_path / "some_board"
        assert "name: Some Board" in manifest.read_text(encoding="utf-8")
    else:
        assert result is None
        assert "name: X" in manifest.read_text(encoding="utf-8")


def test_emit_manifest_preserves_full_config_override(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, "some_board", "source-a")
    path.write_text(path.read_text(encoding="utf-8") + "full_config: false\n", encoding="utf-8")
    assert emit_manifest(dict(_RECORD), source_type="source-a", boards_dir=tmp_path) is not None
    text = path.read_text(encoding="utf-8")
    assert "full_config: false" in text
    # Re-inserted right after the esphome block, keeping key order stable.
    assert text.index("esphome:") < text.index("full_config: false") < text.index("source:")


def test_prune_removed_only_touches_own_source_type(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "gone_own", "source-a")
    _write_manifest(tmp_path, "kept_own", "source-a")
    _write_manifest(tmp_path, "foreign", "source-b")
    _write_manifest(tmp_path, "curated", None)
    removed = prune_removed({"kept_own"}, source_type="source-a", boards_dir=tmp_path)
    assert removed == ["gone_own"]
    assert not (tmp_path / "gone_own").exists()
    assert (tmp_path / "kept_own").is_dir()
    assert (tmp_path / "foreign").is_dir()
    assert (tmp_path / "curated").is_dir()


def test_hash_content_is_stable_sha256() -> None:
    assert hash_content("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
