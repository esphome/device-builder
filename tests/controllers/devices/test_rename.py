"""
Tests for ``DevicesController.rename_device``.

The default path is a thin pass-through to ``esphome rename`` via the
firmware queue (YAML edit -> revalidate -> compile -> OTA install ->
rollback on failure). The dashboard enforces preconditions before
queueing:

- target filename collision — rejected up-front because the CLI would
  happily overwrite an unrelated device's YAML
- same-name renames — rejected up-front, compared against the device's
  real ``esphome.name`` (not the filename stem) so a config whose file
  was slugified away from its name (uploaded ``name: test_1`` ->
  ``test-1.yaml``) can still be renamed to fix the name

An in-place rename — the target filename is the device's own file —
can't go through ``esphome rename`` (it requires a new filename), so it
rewrites the name in place via the config-only path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.yaml import read_yaml_scalar
from esphome_device_builder.models import (
    ErrorCode,
    FirmwareJob,
    JobStatus,
    JobType,
)

from .conftest import MakeControllerFactory

_YAML = """\
esphome:
  name: kitchen
  friendly_name: Kitchen Light

esp32:
  board: esp32dev
"""

# An uploaded config whose file was slugified (``test_1`` -> ``test-1.yaml``)
# while the body keeps the underscore name.
_UNDERSCORE_YAML = """\
esphome:
  name: test_1
  friendly_name: Test_1

esp32:
  board: esp32dev
"""


async def test_rename_target_filename_collision_raises(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Renaming onto a different existing config rejects before any work runs.

    The CLI ``esphome rename`` doesn't check this itself — it would
    happily overwrite the unrelated device's YAML and OTA-install
    the wrong firmware. We have to catch it ourselves at the gate.
    """
    controller = make_controller(tmp_path, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    (tmp_path / "livingroom.yaml").write_text(_YAML, encoding="utf-8")

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
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yaml", new_name="kitchen")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "must differ" in excinfo.value.message


async def test_rename_same_name_compares_real_esphome_name(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A no-op is judged by ``esphome.name``, not the filename stem.

    The file is ``test-1.yaml`` but the device is named ``test_1``;
    renaming to ``test_1`` is the real no-op and must be rejected even
    though it differs from the filename stem.
    """
    controller = make_controller(tmp_path, esphome_cmd=["esphome"])
    (tmp_path / "test-1.yaml").write_text(_UNDERSCORE_YAML, encoding="utf-8")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="test-1.yaml", new_name="test_1")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "must differ" in excinfo.value.message


async def test_rename_same_name_falls_back_to_stem_for_nonlocal_substitution(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An unresolved ``${var}`` name is treated as unknown, comparing on the stem.

    ``esphome.name: ${devicename}`` with no local definition can't be
    resolved here, so the no-op guard falls back to the filename stem
    rather than comparing ``new_name`` against the literal ``${devicename}``.
    """
    controller = make_controller(tmp_path, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text("esphome:\n  name: ${devicename}\n", encoding="utf-8")

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yaml", new_name="kitchen")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    assert "must differ" in excinfo.value.message


async def test_rename_underscore_name_to_hyphen_in_place(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``test_1`` (file ``test-1.yaml``) renames to ``test-1`` in place.

    The slugified filename already matches the new name, so the rename
    rewrites ``esphome.name`` on the same file instead of moving it; the
    file must survive and nothing is queued.
    """
    controller = make_controller(tmp_path)
    config = tmp_path / "test-1.yaml"
    config.write_text(_UNDERSCORE_YAML, encoding="utf-8")

    result = await controller.rename_device(configuration="test-1.yaml", new_name="test-1")

    assert result == {"configuration": "test-1.yaml", "job": None}
    assert config.exists()
    new_content = config.read_text(encoding="utf-8")
    assert read_yaml_scalar(new_content, ("esphome", "name")) == "test-1"
    assert read_yaml_scalar(new_content, ("esphome", "friendly_name")) == "Test_1"
    assert controller._scanner.calls == [("scan",)]


async def test_rename_in_place_with_redundant_path_segments(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A denormalized configuration (``foo/../test-1.yaml``) still reads as in-place.

    ``normpath`` collapses the redundant segment lexically; a textual path
    compare would miss it and the rename would false-collide with the
    device's own file.
    """
    controller = make_controller(tmp_path)
    (tmp_path / "foo").mkdir()
    config = tmp_path / "test-1.yaml"
    config.write_text(_UNDERSCORE_YAML, encoding="utf-8")

    result = await controller.rename_device(configuration="foo/../test-1.yaml", new_name="test-1")

    assert result["job"] is None
    assert config.exists()
    assert read_yaml_scalar(config.read_text(encoding="utf-8"), ("esphome", "name")) == "test-1"


async def test_rename_in_place_ignores_config_only_flag(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An in-place rename rewrites the name even with the default (online) arg.

    ``esphome rename`` can't keep the same filename, so the in-place case
    routes to the config-only rewrite regardless of ``config_only``; the
    firmware queue is never touched.
    """
    controller = make_controller(tmp_path)
    controller._db.firmware = MagicMock()
    controller._db.firmware.rename = AsyncMock()
    config = tmp_path / "test-1.yaml"
    config.write_text(_UNDERSCORE_YAML, encoding="utf-8")

    result = await controller.rename_device(configuration="test-1.yaml", new_name="test-1")

    assert result["job"] is None
    controller._db.firmware.rename.assert_not_awaited()
    assert read_yaml_scalar(config.read_text(encoding="utf-8"), ("esphome", "name")) == "test-1"


async def test_rename_queues_firmware_job(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Pre-conditions clear → firmware queue, response carries the queued job."""
    controller = make_controller(tmp_path, esphome_cmd=["esphome"])
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")

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
    (tmp_path / "kitchen.yaml").write_text(_YAML, encoding="utf-8")
    controller._db.firmware = None

    with pytest.raises(CommandError) as excinfo:
        await controller.rename_device(configuration="kitchen.yaml", new_name="livingroom")

    assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
