"""Coverage for the OTA bootloader-update flag on install / upload."""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.controllers.firmware.cli import build_command
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode, JobType
from tests.controllers.firmware.conftest import FirmwareControllerFactory
from tests.controllers.firmware.conftest import upload_of as _upload_of


def test_build_command_upload_appends_bootloader_after_device() -> None:
    """``--bootloader`` lands on UPLOAD argv after the ``--device`` target."""
    cmd = build_command(
        ["esphome"], JobType.UPLOAD, "kitchen.yaml", "OTA", [], flash_bootloader=True
    )
    assert cmd[-3:] == ["--device", "OTA", "--bootloader"]


def test_build_command_upload_defaults_to_app_flash() -> None:
    """Without the flag, UPLOAD argv is unchanged."""
    cmd = build_command(["esphome"], JobType.UPLOAD, "kitchen.yaml", "OTA", [])
    assert "--bootloader" not in cmd


def test_build_command_non_upload_ignores_bootloader() -> None:
    """The flag exists only on the ``upload`` subparser; INSTALL never gets it."""
    cmd = build_command(
        ["esphome"], JobType.INSTALL, "kitchen.yaml", "OTA", [], flash_bootloader=True
    )
    assert "--bootloader" not in cmd


async def test_upload_bootloader_flag_reaches_job(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware/upload`` with ``bootloader=True`` marks the UPLOAD job."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.upload(configuration="kitchen.yaml", port="OTA", bootloader=True)

    assert job.job_type is JobType.UPLOAD
    assert job.flash_bootloader is True


@pytest.mark.parametrize("port", ["", "/dev/ttyUSB0", "COM3"])
async def test_upload_bootloader_rejects_non_network_port(
    tmp_path: Path, port: str, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A serial / auto-detect target is refused — ``--bootloader`` is OTA-only."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    with pytest.raises(CommandError) as err:
        await controller.upload(configuration="kitchen.yaml", port=port, bootloader=True)

    assert err.value.code == ErrorCode.INVALID_ARGS


async def test_install_bootloader_marks_only_the_upload_half(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``firmware/install`` with ``bootloader=True`` flags the chain's UPLOAD, not the COMPILE."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    compile_job = await controller.install(configuration="kitchen.yaml", bootloader=True)

    assert compile_job.job_type is JobType.COMPILE
    assert compile_job.flash_bootloader is False
    upload = _upload_of(controller, compile_job)
    assert upload.flash_bootloader is True
    assert upload.depends_on == compile_job.job_id


async def test_install_bootloader_rejects_serial_port(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A serial install target with ``bootloader=True`` is refused up front."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    with pytest.raises(CommandError) as err:
        await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0", bootloader=True)

    assert err.value.code == ErrorCode.INVALID_ARGS
    assert not controller.state.jobs
