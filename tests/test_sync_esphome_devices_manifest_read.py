"""Coverage for ``_read_manifest_dict``'s missing / corrupt handling."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from script.sync_esphome_devices import _read_manifest_dict  # type: ignore[import-not-found]


def test_missing_manifest_returns_none_silently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="sync_esphome_devices"):
        assert _read_manifest_dict(tmp_path / "manifest.yaml") is None
    assert caplog.records == []


def test_corrupt_manifest_returns_none_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("key: [unterminated\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="sync_esphome_devices"):
        assert _read_manifest_dict(path) is None
    assert any("Ignoring unreadable manifest" in r.getMessage() for r in caplog.records)


def test_valid_manifest_returns_dict(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("id: foo\n", encoding="utf-8")
    assert _read_manifest_dict(path) == {"id": "foo"}
