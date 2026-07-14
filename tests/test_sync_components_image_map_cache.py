"""Docs-cache invalidation in ``script/sync_components.py`` (#2053)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from script import sync_components  # type: ignore[import-not-found]

_INDEX_ROW = b'["DHT", "/components/sensor/dht/", "dht.svg"]'
_FRESH_ROW = b'["IMU", "/components/sensor/imu/", "imu.svg"]'


@pytest.fixture
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sync_components, "_CACHE_ROOT", tmp_path)
    return tmp_path


def _write_index(cache_root: Path, payload: bytes, *, age: float = 0.0) -> Path:
    cache_file = cache_root / sync_components._DOCS_INDEX_CACHE_NAME
    cache_file.write_bytes(payload)
    if age:
        stamp = time.time() - age
        os.utime(cache_file, (stamp, stamp))
    return cache_file


def test_fresh_cache_skips_the_fetch(cache_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_index(cache_root, _INDEX_ROW)

    def _boom(url: str, **kwargs: object) -> bytes:
        raise AssertionError("fresh cache must not refetch")

    monkeypatch.setattr(sync_components, "_http_get", _boom)
    image_map = sync_components.load_image_map()
    assert image_map["sensor.dht"].endswith("dht.svg")


def test_stale_cache_is_refetched(cache_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_file = _write_index(cache_root, _INDEX_ROW, age=sync_components._DOCS_INDEX_MAX_AGE + 60)
    monkeypatch.setattr(sync_components, "_http_get", lambda url, **kw: _FRESH_ROW)
    image_map = sync_components.load_image_map()
    assert cache_file.read_bytes() == _FRESH_ROW
    assert "sensor.imu" in image_map
    assert "sensor.dht" not in image_map


def test_stale_cache_survives_a_failed_refresh(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_index(cache_root, _INDEX_ROW, age=sync_components._DOCS_INDEX_MAX_AGE + 60)

    def _boom(url: str, **kwargs: object) -> bytes:
        raise OSError("offline")

    monkeypatch.setattr(sync_components, "_http_get", _boom)
    image_map = sync_components.load_image_map()
    assert image_map["sensor.dht"].endswith("dht.svg")


def test_missing_cache_and_failed_fetch_returns_empty(
    cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(url: str, **kwargs: object) -> bytes:
        raise OSError("offline")

    monkeypatch.setattr(sync_components, "_http_get", _boom)
    assert sync_components.load_image_map() == {}


def test_clean_caches_removes_schemas_and_docs_caches(cache_root: Path) -> None:
    schema_dir = cache_root / "esphome-schema-2026.6.0"
    schema_dir.mkdir(parents=True)
    (schema_dir / "schema.json").write_text("{}")
    docs_clone = cache_root / sync_components._DOCS_CLONE_DIR
    docs_clone.mkdir()
    (docs_clone / "index.mdx").write_text("x")
    index_cache = _write_index(cache_root, _INDEX_ROW)
    unrelated = cache_root / "esphome-devices"
    unrelated.mkdir()

    sync_components._clean_caches()

    assert not schema_dir.exists()
    assert not docs_clone.exists()
    assert not index_cache.exists()
    assert unrelated.exists()


def test_clean_caches_tolerates_a_missing_cache_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sync_components, "_CACHE_ROOT", tmp_path / "absent")
    sync_components._clean_caches()
