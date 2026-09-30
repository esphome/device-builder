"""Coverage for passing ``--skip-bootloader`` on OTA app install compiles."""

from __future__ import annotations

from pathlib import Path

from esphome_device_builder.controllers.firmware.cli import (
    build_command,
    effective_skip_bootloader,
    skip_bootloader_supported,
)
from esphome_device_builder.models import FirmwareJob, JobType
from tests.controllers.firmware.conftest import FirmwareControllerFactory
from tests.controllers.firmware.conftest import upload_of as _upload_of


def test_build_command_compile_appends_skip_bootloader() -> None:
    """The flag lands on COMPILE argv only."""
    cmd = build_command(["esphome"], JobType.COMPILE, "kitchen.yaml", "", [], skip_bootloader=True)
    assert cmd[-1] == "--skip-bootloader"


def test_build_command_compile_defaults_to_full_build() -> None:
    cmd = build_command(["esphome"], JobType.COMPILE, "kitchen.yaml", "", [])
    assert "--skip-bootloader" not in cmd


def test_build_command_upload_never_gets_skip_bootloader() -> None:
    """``--skip-bootloader`` exists only on compile and run."""
    cmd = build_command(
        ["esphome"], JobType.UPLOAD, "kitchen.yaml", "OTA", [], skip_bootloader=True
    )
    assert "--skip-bootloader" not in cmd


def test_skip_bootloader_supported_gates_on_the_release_line() -> None:
    """A pinned older esphome must not receive an unknown flag."""
    old = FirmwareJob(
        job_id="a",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        created_at="",
        target_esphome_version="2026.9.2",
    )
    new = FirmwareJob(
        job_id="b",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        created_at="",
        target_esphome_version="2026.10.0",
    )
    assert skip_bootloader_supported(old) is False
    assert skip_bootloader_supported(new) is True
    # The runner gate: an enqueued skip never reaches an older esphome's argv.
    old.skip_bootloader = True
    new.skip_bootloader = True
    assert effective_skip_bootloader(old) is False
    assert effective_skip_bootloader(new) is True
    new.skip_bootloader = False
    assert effective_skip_bootloader(new) is False


async def test_install_ota_marks_the_compile_half(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """An OTA app install compile skips the bootloader; the upload is untouched."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    compile_job = await controller.install(configuration="kitchen.yaml")

    assert compile_job.job_type is JobType.COMPILE
    assert compile_job.skip_bootloader is True
    upload = _upload_of(controller, compile_job)
    assert upload.skip_bootloader is False


async def test_install_to_an_explicit_address_still_skips(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The advanced OTA address card is still an app-only network flash."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    compile_job = await controller.install(configuration="kitchen.yaml", port="192.168.1.50")

    assert compile_job.skip_bootloader is True


async def test_install_bootloader_keeps_the_full_build(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A bootloader flash needs the bootloader built; nothing skips."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    compile_job = await controller.install(configuration="kitchen.yaml", bootloader=True)

    assert compile_job.skip_bootloader is False


async def test_install_serial_keeps_the_full_build(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A serial flash needs the factory image, so the compile stays full."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    compile_job = await controller.install(configuration="kitchen.yaml", port="/dev/ttyUSB0")

    assert compile_job.skip_bootloader is False
