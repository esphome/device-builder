"""
Tests for ``DevicesController.rename_device``.

Three behaviours we explicitly guard against regressing, all
related to keeping the user able to reach the device under its
old name when something goes wrong mid-rename:

- Invalid YAML (``esphome config`` exits non-zero) routes through
  the inline file-level ``_manual_rename`` — no flash needed since
  the device has nothing on it yet.
- Valid YAML routes through the firmware queue (``firmware/rename``)
  so the compile + OTA install runs as a tracked job with live
  output, and the call returns the queued ``FirmwareJob`` for the
  frontend to follow.
- Precheck failures (CLI missing, permission errors, etc.) raise
  a ``CommandError`` instead of silently falling back to the
  file-level path — the silent fallback was the original footgun
  we're protecting against.
"""

from __future__ import annotations

from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import (
    ErrorCode,
    FirmwareJob,
    JobStatus,
    JobType,
)


def _make_controller() -> DevicesController:
    """Build a bare-bones controller with the bits ``rename_device`` touches."""
    controller = DevicesController.__new__(DevicesController)
    controller._db = MagicMock()
    controller._db.settings.rel_path = _FakePath
    controller._scanner = MagicMock()
    controller._scanner.scan = AsyncMock()
    controller._esphome_cmd = ["esphome"]
    return controller


class _FakePath:
    """Minimal Path stand-in for the rename-device tests.

    Override ``_FakePath.existing`` from a test to mark filenames as
    "present on disk"; everything else reports missing. Stringifies
    to ``./<configuration>`` so the rest of the rename code path
    (which only ever wraps the result back into a string) is happy.
    """

    existing: ClassVar[set[str]] = set()

    def __init__(self, configuration: str) -> None:
        self._configuration = configuration

    def __str__(self) -> str:
        return f"./{self._configuration}"

    def exists(self) -> bool:
        return self._configuration in _FakePath.existing


@pytest.fixture(autouse=True)
def _reset_fake_path_existing() -> Any:
    _FakePath.existing = set()
    yield
    _FakePath.existing = set()


@pytest.mark.asyncio
async def test_rename_target_filename_collision_raises(monkeypatch: Any) -> None:
    """Renaming onto an existing config rejects before any work runs.

    The CLI ``esphome rename`` path doesn't check this itself — it
    would happily overwrite the unrelated device's YAML and install
    the wrong firmware. We have to catch it ourselves at the gate.
    """
    controller = _make_controller()
    _FakePath.existing.add("livingroom.yaml")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yaml", new_name="livingroom")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "already exists" in excinfo.value.message


@pytest.mark.asyncio
async def test_rename_same_name_raises() -> None:
    """Renaming a device to its current name rejects up-front.

    A same-name rename is a no-op at the YAML level but every
    downstream branch (manual rewrite, CLI ``esphome rename``)
    would still rewrite the file and (for the CLI path) re-flash.
    Wasted work the caller almost certainly didn't intend —
    ``firmware/install`` is the right command for "flash without
    renaming."
    """
    controller = _make_controller()

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yaml", new_name="kitchen")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "must differ" in excinfo.value.message


@pytest.mark.asyncio
async def test_rename_invalid_yaml_uses_manual_path(monkeypatch: Any) -> None:
    """Invalid config → inline ``_manual_rename`` and ``job: None`` response."""
    controller = _make_controller()

    async def fake_validates(self: Any, config_path: str) -> bool:
        return False

    monkeypatch.setattr(DevicesController, "_yaml_validates", fake_validates)
    calls: list[tuple[str, str]] = []

    def fake_manual(self: Any, configuration: str, new_name: str) -> None:
        calls.append((configuration, new_name))

    monkeypatch.setattr(DevicesController, "_manual_rename", fake_manual)

    result = await controller.rename_device(configuration="kitchen.yaml", new_name="livingroom")

    assert result == {"configuration": "livingroom.yaml", "job": None}
    assert calls == [("kitchen.yaml", "livingroom")]
    controller._scanner.scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_rename_invalid_yaml_collision_raises_command_error(
    monkeypatch: Any,
) -> None:
    """Manual rename's FileExistsError surfaces as INVALID_ARGS."""
    controller = _make_controller()

    async def fake_validates(self: Any, config_path: str) -> bool:
        return False

    monkeypatch.setattr(DevicesController, "_yaml_validates", fake_validates)

    def manual_collision(self: Any, configuration: str, new_name: str) -> None:
        raise FileExistsError("livingroom.yaml")

    monkeypatch.setattr(DevicesController, "_manual_rename", manual_collision)

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yaml", new_name="livingroom")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "already exists" in excinfo.value.message


@pytest.mark.asyncio
async def test_rename_valid_yaml_queues_firmware_job(monkeypatch: Any) -> None:
    """Valid config → firmware queue, response carries the queued job."""
    controller = _make_controller()

    async def fake_validates(self: Any, config_path: str) -> bool:
        return True

    monkeypatch.setattr(DevicesController, "_yaml_validates", fake_validates)

    queued = FirmwareJob(
        job_id="abc123",
        configuration="kitchen.yaml",
        job_type=JobType.RENAME,
        status=JobStatus.QUEUED,
        new_name="livingroom",
    )
    controller._db.firmware = MagicMock()
    controller._db.firmware.rename = AsyncMock(return_value=queued)

    result = await controller.rename_device(configuration="kitchen.yaml", new_name="livingroom")

    controller._db.firmware.rename.assert_awaited_once_with(
        configuration="kitchen.yaml", new_name="livingroom"
    )
    assert result["configuration"] == "livingroom.yaml"
    assert result["job"]["job_id"] == "abc123"
    assert result["job"]["job_type"] == JobType.RENAME
    # Manual rename must NOT have run — old YAML stays untouched
    # while the queued job owns the rollback semantics.
    controller._scanner.scan.assert_not_called()


@pytest.mark.asyncio
async def test_rename_missing_firmware_controller_raises(monkeypatch: Any) -> None:
    """Lifecycle race where firmware controller hasn't started yet."""
    controller = _make_controller()

    async def fake_validates(self: Any, config_path: str) -> bool:
        return True

    monkeypatch.setattr(DevicesController, "_yaml_validates", fake_validates)
    controller._db.firmware = None

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yaml", new_name="livingroom")

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR


@pytest.mark.asyncio
async def test_yaml_validates_returns_false_on_clean_nonzero_exit(
    monkeypatch: Any,
) -> None:
    """``esphome config`` exited non-zero → False (the only "invalid" signal)."""
    controller = _make_controller()

    fake_proc = MagicMock()
    fake_proc.wait = AsyncMock(return_value=1)
    create_calls: list[tuple[Any, ...]] = []

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        create_calls.append((args, kwargs))
        return fake_proc

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.create_subprocess_exec",
        fake_create,
    )

    result = await controller._yaml_validates("./kitchen.yaml")

    assert result is False
    assert create_calls, "subprocess was not invoked"


@pytest.mark.asyncio
async def test_yaml_validates_propagates_unexpected_exception(
    monkeypatch: Any,
) -> None:
    """CLI missing / permission errors must NOT silently fall back.

    Returning ``False`` on an unexpected exception used to route a
    valid config into the file-level rename path — exactly the
    "rename without a successful flash" footgun the safety rewrite
    is meant to prevent. We now raise so the rename is rejected
    with a real reason instead.
    """
    controller = _make_controller()

    async def fake_create(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("esphome")

    monkeypatch.setattr(
        "esphome_device_builder.controllers.devices.controller.create_subprocess_exec",
        fake_create,
    )

    with pytest.raises(CommandError) as excinfo:
        await controller._yaml_validates("./kitchen.yaml")

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
