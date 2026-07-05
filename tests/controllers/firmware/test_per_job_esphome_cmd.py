"""Coverage for the per-job esphome interpreter seam."""

from __future__ import annotations

from esphome_device_builder.models import JobType
from esphome_device_builder.models.firmware import FirmwareJob
from tests.controllers.firmware.conftest import BareFirmwareControllerFactory


def test_build_command_prefers_the_per_call_esphome_cmd(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """An explicit ``esphome_cmd`` overrides ``state.esphome_cmd`` in the argv."""
    controller = bare_firmware_controller_factory(esphome_cmd=["installed", "-m", "esphome"])

    cmd = controller._build_command(
        JobType.COMPILE, "kitchen.yaml", "", esphome_cmd=["venv/bin/esphome"]
    )

    assert cmd[:2] == ["venv/bin/esphome", "--dashboard"]


def test_build_command_falls_back_to_state_when_unset(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """``None`` / empty ``esphome_cmd`` keeps the controller-wide invocation."""
    controller = bare_firmware_controller_factory(esphome_cmd=["installed", "-m", "esphome"])

    for override in (None, []):
        cmd = controller._build_command(JobType.COMPILE, "kitchen.yaml", "", esphome_cmd=override)
        assert cmd[:3] == ["installed", "-m", "esphome"]


async def test_resolve_esphome_cmd_defaults_to_installed(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """The resolver seam returns the installed invocation for a plain job."""
    controller = bare_firmware_controller_factory(esphome_cmd=["installed", "-m", "esphome"])
    job = FirmwareJob(job_id="j1", configuration="kitchen.yaml", job_type=JobType.COMPILE)

    assert await controller._resolve_esphome_cmd(job) == ["installed", "-m", "esphome"]
