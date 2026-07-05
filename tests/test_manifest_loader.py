"""Unit tests for ``load_manifest_dict`` in ``script/_manifest.py``."""

from __future__ import annotations

from pathlib import Path

import pytest

from script._manifest import ManifestError, load_manifest_dict  # type: ignore[import-not-found]


def test_returns_mapping(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("name: dht\npins: []\n", encoding="utf-8")
    assert load_manifest_dict(path) == {"name": "dht", "pins": []}


def test_yaml_error_reason(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("key: [unterminated\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="invalid YAML"):
        load_manifest_dict(path)


def test_non_mapping_reason(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="not a YAML mapping"):
        load_manifest_dict(path)


def test_os_error_propagates(tmp_path: Path) -> None:
    # A missing file surfaces as OSError (FileNotFoundError), not
    # ManifestError, so callers can tell "can't read" apart from "bad content".
    with pytest.raises(FileNotFoundError):
        load_manifest_dict(tmp_path / "missing.yaml")
