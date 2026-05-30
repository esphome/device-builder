"""
Tests for ``DevicesController.rename_device``.

The handler is now a thin pass-through to ``esphome rename`` via
the firmware queue. The CLI owns the full atomic flow (YAML edit
→ revalidate → compile → OTA install → rollback on failure), so
the dashboard only enforces two preconditions before queueing:

- target filename collision — rejected up-front because the CLI
  would happily overwrite an unrelated device's YAML
- same-name renames — rejected up-front because they're a no-op
  at the YAML level but still queue a real compile + flash

What we used to also do — pre-validate via ``esphome config`` and
fall back to a file-level rename when validation failed — is gone.
The fallback silently renamed the YAML on disk while the running
firmware kept broadcasting the old hostname, leaving dashboard
label and device state diverged with no error to the user. Rename
now refuses cleanly when the CLI's own validation fails so the
user fixes the YAML and retries.
"""

from __future__ import annotations

from pathlib import Path
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

from .conftest import MakeControllerFactory


def _wire_fake_path(controller: DevicesController) -> None:
    """Swap the factory's tmp_path-based ``rel_path`` for the ``_FakePath`` shim."""
    controller._db.settings.rel_path = _FakePath


class _FakePath:
    """Path stand-in: ``existing`` set drives ``.exists()`` results."""

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


async def test_rename_target_filename_collision_raises(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Renaming onto an existing config rejects before any work runs.

    The CLI ``esphome rename`` doesn't check this itself — it would
    happily overwrite the unrelated device's YAML and OTA-install
    the wrong firmware. We have to catch it ourselves at the gate.
    """
    controller = make_controller(tmp_path, esphome_cmd=["esphome"])
    _wire_fake_path(controller)
    _FakePath.existing.add("livingroom.yaml")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yaml", new_name="livingroom")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "already exists" in excinfo.value.message


async def test_rename_same_name_raises(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Renaming a device to its current name rejects up-front.

    A no-op at the YAML level but the CLI would still re-flash —
    wasted work the caller almost certainly didn't intend.
    ``firmware/install`` is the right command for "flash without
    renaming."
    """
    controller = make_controller(tmp_path, esphome_cmd=["esphome"])
    _wire_fake_path(controller)

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yaml", new_name="kitchen")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "must differ" in excinfo.value.message


async def test_rename_same_name_raises_for_yml_extension(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Stem comparison catches ``kitchen.yml`` → ``new_name=kitchen``.

    The literal filenames differ (``kitchen.yml`` vs the
    constructed ``kitchen.yaml``) but the device's mDNS hostname
    comes from the stem and stays the same either way, so the
    rename would still be a no-op rewrite + redundant flash. A
    naive ``new_filename == configuration`` check would let this
    through; comparing on stems catches it.
    """
    controller = make_controller(tmp_path, esphome_cmd=["esphome"])
    _wire_fake_path(controller)

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yml", new_name="kitchen")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "must differ" in excinfo.value.message


async def test_rename_queues_firmware_job(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Pre-conditions clear → firmware queue, response carries the queued job."""
    controller = make_controller(tmp_path, esphome_cmd=["esphome"])
    _wire_fake_path(controller)

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
    # No file-level rename; the queued job owns the rename + rollback.
    assert controller._scanner.calls == []


async def test_rename_missing_firmware_controller_raises(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Lifecycle race where firmware controller hasn't started yet."""
    controller = make_controller(tmp_path, esphome_cmd=["esphome"])
    _wire_fake_path(controller)
    controller._db.firmware = None

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yaml", new_name="livingroom")

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
