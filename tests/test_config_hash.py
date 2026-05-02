"""Tests for ``helpers/config_hash.compute_yaml_config_hash``.

The hash comes out of ``<build_path>/build_info.json``, which
ESPHome writes during every successful build (including
``--only-generate``). The dashboard already runs ``--only-generate``
on YAML add / edit, so by the time we'd read the hash the file is
in place. These tests stub ``StorageJSON.load`` to point at a
``build_path`` we control, write a representative ``build_info.json``
there, and verify the helper round-trips the value into the
mDNS-canonical 8-char lowercase hex format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.helpers.config_hash import compute_yaml_config_hash


def _stub_storage(monkeypatch: Any, build_path: Path | None) -> None:
    """Make ``StorageJSON.load`` return a stub pointing at *build_path*.

    ``None`` simulates "device has never been compiled" — the
    ``StorageJSON.load`` itself returns None for missing sidecars.
    """
    if build_path is None:
        load_mock = MagicMock(return_value=None)
    else:
        storage = MagicMock()
        storage.build_path = build_path
        load_mock = MagicMock(return_value=storage)
    monkeypatch.setattr(
        "esphome_device_builder.helpers.config_hash.StorageJSON.load",
        load_mock,
    )
    monkeypatch.setattr(
        "esphome_device_builder.helpers.config_hash.ext_storage_path",
        lambda _filename: Path("/unused/by/the/stub.json"),
    )


def _write_build_info(build_path: Path, **fields: Any) -> None:
    """Write a ``build_info.json`` with the given fields.

    Defaults match what ESPHome's writer emits (see
    ``esphome.writer.copy_src_tree``): a 32-bit unsigned int
    ``config_hash``, a unix ``build_time`` etc. Tests override the
    one or two fields they care about.
    """
    build_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "config_hash": 0xDEADBEEF,
        "build_time": 1700000000,
        "build_time_str": "2025-11-14 12:00:00 -0500",
        "esphome_version": "2026.5.0-dev",
    }
    payload.update(fields)
    (build_path / "build_info.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_returns_canonical_hex_from_build_info(tmp_path: Path, monkeypatch: Any) -> None:
    """``build_info.json`` round-trips into the 8-char hex shape."""
    build_path = tmp_path / ".esphome" / "build" / "kitchen"
    # 0x5a94a12d is the value the firmware actually broadcasts via
    # mDNS for a representative Apollo float-monitor YAML — the
    # original bug report. Locking in this exact shape protects
    # against accidentally returning the decimal or hex-with-0x
    # forms.
    _write_build_info(build_path, config_hash=0x5A94A12D)
    _stub_storage(monkeypatch, build_path)

    result = await compute_yaml_config_hash(tmp_path / "kitchen.yaml")

    assert result == "5a94a12d"


@pytest.mark.asyncio
async def test_returns_zero_padded_low_value(tmp_path: Path, monkeypatch: Any) -> None:
    """Small ints get padded to 8 chars so the comparison is straight strcmp."""
    build_path = tmp_path / "build"
    _write_build_info(build_path, config_hash=0x42)
    _stub_storage(monkeypatch, build_path)

    result = await compute_yaml_config_hash(tmp_path / "kitchen.yaml")

    assert result == "00000042"


@pytest.mark.asyncio
async def test_returns_none_when_storage_missing(tmp_path: Path, monkeypatch: Any) -> None:
    """No sidecar → no build path → no hash. Caller falls back to mtime."""
    _stub_storage(monkeypatch, None)

    result = await compute_yaml_config_hash(tmp_path / "ghost.yaml")

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_build_info_missing(tmp_path: Path, monkeypatch: Any) -> None:
    """Sidecar points at a build dir but ``--only-generate`` hasn't run yet."""
    build_path = tmp_path / "build"
    build_path.mkdir(parents=True)
    _stub_storage(monkeypatch, build_path)

    result = await compute_yaml_config_hash(tmp_path / "kitchen.yaml")

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_for_corrupt_json(tmp_path: Path, monkeypatch: Any) -> None:
    """Corrupt build_info.json → None, no exception bubbles up."""
    build_path = tmp_path / "build"
    build_path.mkdir(parents=True)
    (build_path / "build_info.json").write_text("{this is not json", encoding="utf-8")
    _stub_storage(monkeypatch, build_path)

    result = await compute_yaml_config_hash(tmp_path / "kitchen.yaml")

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_hash_missing_or_wrong_type(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Defensive: build_info.json shape changes shouldn't crash us."""
    build_path = tmp_path / "build"
    build_path.mkdir(parents=True)
    (build_path / "build_info.json").write_text(
        json.dumps({"build_time": 0}),  # no config_hash key at all
        encoding="utf-8",
    )
    _stub_storage(monkeypatch, build_path)
    assert await compute_yaml_config_hash(tmp_path / "kitchen.yaml") is None

    (build_path / "build_info.json").write_text(
        json.dumps({"config_hash": "not-an-int"}),
        encoding="utf-8",
    )
    assert await compute_yaml_config_hash(tmp_path / "kitchen.yaml") is None
