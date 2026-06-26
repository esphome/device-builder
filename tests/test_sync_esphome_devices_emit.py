"""Pin ``_emit_manifest``'s collision-skip and overwrite behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from script.sync_esphome_devices import _emit_manifest  # type: ignore[import-not-found]


def _write_board(boards_dir: Path, board_id: str, text: str) -> Path:
    target = boards_dir / board_id
    target.mkdir(parents=True)
    manifest = target / "manifest.yaml"
    manifest.write_text(text, encoding="utf-8")
    return manifest


def test_skips_hand_curated_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing manifest with no ``source.type`` is left untouched (slug collision)."""
    monkeypatch.setattr("script.sync_esphome_devices._BOARDS_DIR", tmp_path)
    original = "id: myboard\nname: Hand Curated\n# no source block\n"
    manifest = _write_board(tmp_path, "myboard", original)

    result = _emit_manifest({"id": "myboard", "name": "Upstream"}, MagicMock())

    assert result is None
    assert manifest.read_text(encoding="utf-8") == original


def test_preserves_unparsable_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing but unparsable manifest is preserved, not clobbered."""
    monkeypatch.setattr("script.sync_esphome_devices._BOARDS_DIR", tmp_path)
    broken = "{{{ this is not: valid yaml ::::\n"
    manifest = _write_board(tmp_path, "myboard", broken)

    result = _emit_manifest({"id": "myboard", "name": "Upstream"}, MagicMock())

    assert result is None
    assert manifest.read_text(encoding="utf-8") == broken


def test_overwrites_imported_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An imported manifest (``source.type``) is rewritten from the fresh record."""
    monkeypatch.setattr("script.sync_esphome_devices._BOARDS_DIR", tmp_path)
    prior = yaml.safe_dump(
        {"id": "myboard", "name": "Old", "source": {"type": "esphome-devices", "remote_id": "X"}}
    )
    manifest = _write_board(tmp_path, "myboard", prior)
    record: dict[str, Any] = {
        "id": "myboard",
        "name": "New",
        "source": {"type": "esphome-devices", "remote_id": "X"},
    }

    result = _emit_manifest(record, MagicMock())

    assert result is not None
    assert yaml.safe_load(manifest.read_text(encoding="utf-8"))["name"] == "New"
