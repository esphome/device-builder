"""Loading and saving the ignored-devices file."""

from __future__ import annotations

import json
import logging
import stat
import sys
from pathlib import Path

import pytest

from esphome_device_builder.controllers.devices._ignored_devices_store import (
    ignored_devices_store,
)
from esphome_device_builder.helpers.storage import Store

from .conftest import MakeControllerFactory


def _store(tmp_path: Path) -> Store[set[str]]:
    return ignored_devices_store(tmp_path / "ignored-devices.json", lambda _cb: None)


@pytest.mark.parametrize(
    ("raw", "expected", "warning"),
    [
        pytest.param(None, None, None, id="missing"),
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
    expected: set[str] | None,
    warning: str | None,
) -> None:
    """A missing file loads as nothing; an unusable one warns once and loads empty."""
    if raw is not None:
        (tmp_path / "ignored-devices.json").write_bytes(raw)
    caplog.set_level(logging.WARNING)

    assert await _store(tmp_path).async_load() == expected

    assert len(caplog.records) == (warning is not None)
    if warning:
        assert warning in caplog.records[0].message


async def test_controller_load_mutates_the_set_in_place(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A pre-load capture of ``__contains__`` sees post-load entries."""
    ctrl = make_controller(tmp_path)
    ctrl.state.ignored_devices.add("stale")
    captured_contains = ctrl.state.ignored_devices.__contains__
    (tmp_path / "ignored-devices.json").write_bytes(b'{"ignored_devices": ["one", "two"]}')

    await ctrl._load_ignored_devices()

    assert captured_contains("one")
    assert captured_contains("two")
    assert not captured_contains("stale")


async def test_save_round_trips_through_the_legacy_file_shape(tmp_path: Path) -> None:
    """A flushed save writes the legacy dashboard's shape, world-readable, and loads back."""
    store = _store(tmp_path)
    store.async_delay_save(lambda: {"kitchen", "garage"})
    await store.async_save_now()

    assert json.loads(store.path.read_bytes()) == {"ignored_devices": ["garage", "kitchen"]}
    if sys.platform != "win32":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o644
    assert await store.async_load() == {"kitchen", "garage"}
