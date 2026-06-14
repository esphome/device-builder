"""Tests for ``PreferencesStore`` — RAM-canonical prefs + sidecar migration."""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest

from esphome_device_builder.controllers.config import _load_metadata, _save_metadata
from esphome_device_builder.controllers.config._preferences_store import (
    _STORE_FILENAME,
    PreferencesStore,
)
from esphome_device_builder.models import UserPreferences
from esphome_device_builder.models.preferences import ExperienceLevel, Theme


def _make_store(tmp_path: Path) -> PreferencesStore:
    """Build a store anchored at *tmp_path* with a noop shutdown register."""
    return PreferencesStore(tmp_path, lambda _cb: None)


_SAMPLE = UserPreferences(
    remote_compute_only=True,
    experience_level=ExperienceLevel.YAML,
    theme=Theme.DARK,
)


async def test_async_load_with_no_files_keeps_defaults(tmp_path: Path) -> None:
    """Fresh install: no files, defaults in RAM, nothing written."""
    store = _make_store(tmp_path)
    await store.async_load()
    assert store.snapshot() == UserPreferences()
    assert not (tmp_path / _STORE_FILENAME).exists()
    assert not (tmp_path / ".device-builder.json").exists()


async def test_async_load_migrates_preferences_from_sidecar(tmp_path: Path) -> None:
    """First run: ``_preferences`` moves to the dedicated file, other keys stay."""
    await asyncio.to_thread(
        _save_metadata,
        tmp_path,
        {
            "_preferences": _SAMPLE.to_dict(),
            "_labels": [{"id": "abc", "name": "Bedroom"}],
            "kitchen.yaml": {"board_id": "esp32"},
        },
    )
    store = _make_store(tmp_path)
    await store.async_load()

    assert store.snapshot() == _SAMPLE
    assert (tmp_path / _STORE_FILENAME).exists()
    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    assert "_preferences" not in shared
    assert shared["_labels"] == [{"id": "abc", "name": "Bedroom"}]
    assert shared["kitchen.yaml"] == {"board_id": "esp32"}


async def test_async_load_leaves_corrupt_sidecar_blob_in_place(tmp_path: Path) -> None:
    """A malformed sidecar ``_preferences`` blob is preserved, not migrated/stripped.

    Falling back to defaults *and* destroying the source would lose recoverable
    data; instead leave the blob in the sidecar and don't write a dedicated file.
    """
    await asyncio.to_thread(_save_metadata, tmp_path, {"_preferences": [1, 2, 3]})
    store = _make_store(tmp_path)
    await store.async_load()

    assert store.snapshot() == UserPreferences()
    assert not (tmp_path / _STORE_FILENAME).exists()
    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    assert shared["_preferences"] == [1, 2, 3]


async def test_async_load_skips_migration_when_dedicated_file_exists(tmp_path: Path) -> None:
    """An existing dedicated file wins; the sidecar ``_preferences`` is left alone."""
    new = UserPreferences(theme=Theme.LIGHT)
    (tmp_path / _STORE_FILENAME).write_bytes(orjson.dumps(new.to_dict()))
    await asyncio.to_thread(_save_metadata, tmp_path, {"_preferences": _SAMPLE.to_dict()})

    store = _make_store(tmp_path)
    await store.async_load()

    assert store.snapshot() == new
    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    assert shared["_preferences"] == _SAMPLE.to_dict()


async def test_async_load_no_preferences_key_keeps_defaults(tmp_path: Path) -> None:
    """A sidecar with other keys but no ``_preferences`` migrates nothing."""
    await asyncio.to_thread(_save_metadata, tmp_path, {"kitchen.yaml": {"board_id": "esp32"}})
    store = _make_store(tmp_path)
    await store.async_load()

    assert store.snapshot() == UserPreferences()
    assert not (tmp_path / _STORE_FILENAME).exists()
    shared = await asyncio.to_thread(_load_metadata, tmp_path)
    assert shared["kitchen.yaml"] == {"board_id": "esp32"}


async def test_async_load_is_idempotent(tmp_path: Path) -> None:
    """A second load reads the dedicated file; the sidecar stays as migration left it."""
    await asyncio.to_thread(_save_metadata, tmp_path, {"_preferences": _SAMPLE.to_dict()})
    first = _make_store(tmp_path)
    await first.async_load()
    shared_after_first = await asyncio.to_thread(_load_metadata, tmp_path)

    second = _make_store(tmp_path)
    await second.async_load()
    assert second.snapshot() == first.snapshot() == _SAMPLE
    assert await asyncio.to_thread(_load_metadata, tmp_path) == shared_after_first


async def test_async_load_preserves_corrupt_dedicated_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt dedicated file is renamed aside (not erased) and logged."""
    (tmp_path / _STORE_FILENAME).write_bytes(b"{not valid json")
    store = _make_store(tmp_path)
    with caplog.at_level("ERROR"):
        await store.async_load()
    assert store.snapshot() == UserPreferences()
    assert any("undecodable" in r.message for r in caplog.records)
    # Original preserved for recovery; the live file is gone so the next save
    # can't overwrite a recoverable-but-corrupt read.
    assert (tmp_path / (_STORE_FILENAME + ".corrupt")).read_bytes() == b"{not valid json"
    assert not (tmp_path / _STORE_FILENAME).exists()


async def test_async_load_preserves_non_dict_dedicated_file(tmp_path: Path) -> None:
    """A dedicated file holding a non-object JSON value is preserved aside."""
    (tmp_path / _STORE_FILENAME).write_bytes(b"[1, 2, 3]")
    store = _make_store(tmp_path)
    await store.async_load()
    assert store.snapshot() == UserPreferences()
    assert (tmp_path / (_STORE_FILENAME + ".corrupt")).read_bytes() == b"[1, 2, 3]"


async def test_async_load_preserves_invalid_shape_dedicated_file(tmp_path: Path) -> None:
    """A dedicated file whose object fails decode is preserved aside."""
    raw = b'{"experience_level": "bogus"}'
    (tmp_path / _STORE_FILENAME).write_bytes(raw)
    store = _make_store(tmp_path)
    await store.async_load()
    assert store.snapshot() == UserPreferences()
    assert (tmp_path / (_STORE_FILENAME + ".corrupt")).read_bytes() == raw


async def test_round_trip_after_migration(tmp_path: Path) -> None:
    """Migrate, mutate, flush, reload: the latest state survives on disk."""
    await asyncio.to_thread(_save_metadata, tmp_path, {"_preferences": _SAMPLE.to_dict()})
    first = _make_store(tmp_path)
    await first.async_load()
    first.update({"theme": Theme.LIGHT})
    await first._store.async_save_now()

    second = _make_store(tmp_path)
    await second.async_load()
    assert second.snapshot().theme == Theme.LIGHT
    assert second.snapshot().experience_level == ExperienceLevel.YAML
