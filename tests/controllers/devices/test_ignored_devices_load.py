"""Defensive loading of the ignored-devices file.

``_load_ignored_devices`` runs once at controller bootstrap. A
corrupt or unexpectedly-shaped file used to take down the whole
controller; now it should log a warning and keep the in-memory set
empty so the rest of the dashboard starts normally.

Each case patches ``ignored_devices_storage_path`` so the test
controls exactly what bytes the loader sees, and runs the sync
method through ``asyncio.to_thread`` to mirror the production call
path (``DevicesController.start`` schedules it on the executor).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.devices._state import DevicesState


@pytest.fixture
def _stub_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DevicesController:
    """Bare controller pointing the loader at a per-test ignored-devices path.

    Sidesteps the full ``__init__`` chain (DeviceBuilder, scanners,
    monitors) — the test exercises a single sync method, so the
    surrounding controllers are dead weight. Patches the upstream
    ``ignored_devices_storage_path`` getter on the devices module so
    the loader reads from ``tmp_path``.
    """
    storage_path = tmp_path / "ignored-devices.json"
    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.importable.ignored_devices_storage_path",
        lambda: storage_path,
    )
    ctrl = DevicesController.__new__(DevicesController)
    ctrl._db = MagicMock()  # type: ignore[attr-defined]
    ctrl.state = DevicesState()
    ctrl.state.ignored_devices = {"will-be-overwritten"}
    return ctrl


async def _load(ctrl: DevicesController) -> None:
    """Run the sync loader on the executor to match production."""
    await asyncio.to_thread(ctrl._load_ignored_devices)


async def test_missing_file_starts_empty(
    _stub_controller: DevicesController, caplog: pytest.LogCaptureFixture
) -> None:
    """No file on disk → empty set, no warnings (the ``FileNotFoundError`` path is silent)."""
    caplog.set_level(logging.WARNING, "esphome_device_builder.controllers.devices")
    await _load(_stub_controller)
    # The fixture pre-populated a sentinel value to prove the loader
    # leaves the in-memory set alone when there's nothing on disk.
    assert _stub_controller.state.ignored_devices == {"will-be-overwritten"}
    assert caplog.records == []


async def test_corrupt_json_resets_with_warning(
    _stub_controller: DevicesController,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bad JSON logs a warning and keeps the in-memory set empty."""
    (tmp_path / "ignored-devices.json").write_bytes(b"{not-json")
    caplog.set_level(logging.WARNING, "esphome_device_builder.controllers.devices")
    await _load(_stub_controller)
    assert _stub_controller.state.ignored_devices == {"will-be-overwritten"}
    assert any("corrupt" in r.message for r in caplog.records)


async def test_top_level_not_object_resets_with_warning(
    _stub_controller: DevicesController,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-object top-level payload triggers the dict-shape guard."""
    (tmp_path / "ignored-devices.json").write_bytes(b'["not", "a", "dict"]')
    caplog.set_level(logging.WARNING, "esphome_device_builder.controllers.devices")
    await _load(_stub_controller)
    assert _stub_controller.state.ignored_devices == {"will-be-overwritten"}
    assert any("isn't a JSON object" in r.message for r in caplog.records)


async def test_non_list_field_resets_with_warning(
    _stub_controller: DevicesController,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dict whose ``ignored_devices`` isn't a list resets to empty + warns."""
    (tmp_path / "ignored-devices.json").write_bytes(
        b'{"ignored_devices": "kitchen"}',
    )
    caplog.set_level(logging.WARNING, "esphome_device_builder.controllers.devices")
    await _load(_stub_controller)
    assert _stub_controller.state.ignored_devices == set()
    assert any("non-list" in r.message for r in caplog.records)


async def test_mixed_entry_types_filtered_to_strings(
    _stub_controller: DevicesController, tmp_path: Path
) -> None:
    """Non-string entries are silently dropped — only strings survive."""
    (tmp_path / "ignored-devices.json").write_bytes(
        b'{"ignored_devices": ["kitchen", 42, null, "garage"]}',
    )
    await _load(_stub_controller)
    assert _stub_controller.state.ignored_devices == {"kitchen", "garage"}


async def test_happy_path(_stub_controller: DevicesController, tmp_path: Path) -> None:
    """A well-formed file loads cleanly into the in-memory set."""
    (tmp_path / "ignored-devices.json").write_bytes(
        b'{"ignored_devices": ["one", "two", "three"]}',
    )
    await _load(_stub_controller)
    assert _stub_controller.state.ignored_devices == {"one", "two", "three"}


async def test_loader_mutates_set_in_place_for_pre_captured_contains(
    _stub_controller: DevicesController, tmp_path: Path
) -> None:
    """A pre-load capture of ``__contains__`` sees post-load entries."""
    captured_contains = _stub_controller.state.ignored_devices.__contains__
    original_set = _stub_controller.state.ignored_devices
    (tmp_path / "ignored-devices.json").write_bytes(
        b'{"ignored_devices": ["one", "two"]}',
    )
    await _load(_stub_controller)
    assert _stub_controller.state.ignored_devices is original_set
    assert captured_contains("one")
    assert captured_contains("two")
    assert not captured_contains("three")
