"""Defensive loading of the ignored-devices file into the in-place set."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from esphome_device_builder.controllers.devices._ignored_devices_store import (
    IgnoredDevicesStore,
)


def _store(tmp_path: Path, names: set[str]) -> IgnoredDevicesStore:
    return IgnoredDevicesStore(tmp_path / "ignored-devices.json", names, lambda _cb: None)


@pytest.mark.parametrize(
    ("raw", "expected", "warning"),
    [
        pytest.param(None, {"seed"}, None, id="missing"),
        pytest.param(b"{not-json", set(), "corrupt", id="corrupt"),
        pytest.param(b'["not", "a", "dict"]', set(), "non-mapping", id="not_object"),
        pytest.param(b'{"ignored_devices": "kitchen"}', set(), "non-list", id="non_list"),
        pytest.param(
            b'{"ignored_devices": ["kitchen", 42, null, "garage"]}',
            {"kitchen", "garage"},
            None,
            id="mixed",
        ),
        pytest.param(
            b'{"ignored_devices": ["one", "two", "three"]}',
            {"one", "two", "three"},
            None,
            id="happy",
        ),
    ],
)
async def test_load_lands_each_on_disk_shape(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    raw: bytes | None,
    expected: set[str],
    warning: str | None,
) -> None:
    """A missing file leaves the set alone; an unusable one warns and empties it."""
    if raw is not None:
        (tmp_path / "ignored-devices.json").write_bytes(raw)
    caplog.set_level(logging.WARNING, "esphome_device_builder.controllers.devices")
    names = {"seed"}

    await _store(tmp_path, names).async_load()

    assert names == expected
    assert [r.message for r in caplog.records if warning and warning in r.message] == (
        [caplog.records[0].message] if warning else []
    )
    assert bool(caplog.records) is (warning is not None)


async def test_loader_mutates_set_in_place_for_pre_captured_contains(tmp_path: Path) -> None:
    """A pre-load capture of ``__contains__`` sees post-load entries."""
    names: set[str] = set()
    captured_contains = names.__contains__
    (tmp_path / "ignored-devices.json").write_bytes(b'{"ignored_devices": ["one", "two"]}')

    await _store(tmp_path, names).async_load()

    assert captured_contains("one")
    assert captured_contains("two")
    assert not captured_contains("three")


async def test_save_round_trips_through_the_legacy_file_shape(tmp_path: Path) -> None:
    """A flushed save writes the legacy dashboard's shape and loads back into a fresh set."""
    names = {"garage", "kitchen"}
    store = _store(tmp_path, names)
    store.save()
    await store.async_save_now()

    assert (tmp_path / "ignored-devices.json").read_bytes().decode() == (
        '{\n  "ignored_devices": [\n    "garage",\n    "kitchen"\n  ]\n}'
    )
    reloaded: set[str] = set()
    await _store(tmp_path, reloaded).async_load()
    assert reloaded == names
