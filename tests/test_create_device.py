"""Tests for the ``devices/create`` command path.

Regression coverage for #81: the three user-correctable failures in
``create_device`` (name collision, empty name, unknown board_id) must
arrive at the WS dispatcher as ``CommandError(INVALID_ARGS, …)`` so
the wizard can show a specific message instead of the generic
``Command failed`` fallback the WS layer emits for any other
exception.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers import devices as devices_module
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode


def _make_controller(tmp_path: Any) -> DevicesController:
    """Stand up a controller without booting the full DeviceBuilder."""
    ctrl = DevicesController.__new__(DevicesController)
    ctrl._db = MagicMock()
    ctrl._db.settings.rel_path = lambda name: tmp_path / name
    # ``_db.boards`` is truthy in production; tests that exercise the
    # board lookup override ``get_board`` with an ``AsyncMock``.
    ctrl._db.boards = MagicMock()
    ctrl._scanner = MagicMock()
    ctrl._scanner.scan = AsyncMock()
    ctrl._state_monitor = MagicMock()
    return ctrl


async def test_create_device_translates_file_exists_to_command_error(
    tmp_path: Any,
) -> None:
    """Re-creating an existing config raises ``INVALID_ARGS``, not ``INTERNAL_ERROR``.

    Without the typed error, the WS dispatcher falls back to a generic
    "Command failed" and the wizard can't tell the user the name is
    already taken — they just see a 500-equivalent.
    """
    ctrl = _make_controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: kitchen\n", "utf-8")

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="kitchen")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "kitchen.yaml already exists" in excinfo.value.message
    # Nothing should hit the scanner when the pre-flight check fails.
    ctrl._scanner.scan.assert_not_called()


async def test_create_device_rejects_empty_name(tmp_path: Any) -> None:
    """Whitespace-only names produce ``INVALID_ARGS`` instead of a bare ``ValueError``."""
    ctrl = _make_controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="   ")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "name is required" in excinfo.value.message
    ctrl._scanner.scan.assert_not_called()


async def test_create_device_rejects_unknown_board_id(tmp_path: Any) -> None:
    """An unknown ``board_id`` produces ``INVALID_ARGS`` and names the bad id."""
    ctrl = _make_controller(tmp_path)
    ctrl._db.boards.get_board = AsyncMock(return_value=None)

    with pytest.raises(CommandError) as excinfo:
        await ctrl.create_device(name="kitchen", board_id="bogus-board")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "bogus-board" in excinfo.value.message
    ctrl._scanner.scan.assert_not_called()


async def test_create_device_writes_stub_yaml_and_scans(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path with no board / no file_content: stub YAML lands on disk and scan fires."""
    storage_path = tmp_path / "storage.json"
    monkeypatch.setattr(devices_module, "ext_storage_path", lambda _filename: storage_path)
    monkeypatch.setattr(devices_module, "set_device_metadata", lambda *args, **kwargs: None)
    ctrl = _make_controller(tmp_path)
    # Catalog lookups must return ``None`` so the derive-from-yaml
    # branch leaves ``board`` unset; otherwise ``StorageJSON``'s
    # ``target_platform`` would receive a ``MagicMock``.
    ctrl._db.boards.find_by_pio_board = MagicMock(return_value=None)
    ctrl._db.boards.find_by_platform_variant = MagicMock(return_value=None)

    result = await ctrl.create_device(name="kitchen")

    assert result.configuration == "kitchen.yaml"
    yaml_path = tmp_path / "kitchen.yaml"
    assert yaml_path.read_text("utf-8").startswith("esphome:\n  name: kitchen\n")
    assert storage_path.exists()
    ctrl._scanner.scan.assert_awaited_once()
