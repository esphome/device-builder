"""Coverage for the per-job esphome interpreter seam."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from esphome_device_builder.controllers.firmware.controller import _installed_esphome_version
from esphome_device_builder.controllers.remote_build.env_provisioner import EnvProvisionError
from esphome_device_builder.models import JobType
from esphome_device_builder.models.firmware import FirmwareJob
from tests.controllers.firmware.conftest import BareFirmwareControllerFactory


def _remote_job(target_esphome_version: str) -> FirmwareJob:
    return FirmwareJob(
        job_id="j1",
        configuration="kitchen.yaml",
        job_type=JobType.COMPILE,
        target_esphome_version=target_esphome_version,
    )


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


async def test_resolve_esphome_cmd_provisions_for_mismatched_target(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """A remote job targeting a different version resolves to the provisioned venv."""
    controller = bare_firmware_controller_factory(
        esphome_cmd=["installed", "-m", "esphome"], with_mock_db=True
    )
    provisioner = controller._db.remote_build_receiver.state.env_provisioner
    provisioner.provision = AsyncMock(return_value=["venv/bin/python", "-m", "esphome"])

    cmd = await controller._resolve_esphome_cmd(_remote_job("2026.5.0"))

    assert cmd == ["venv/bin/python", "-m", "esphome"]
    provisioner.provision.assert_awaited_once_with("2026.5.0")


def _clean_job(target_esphome_version: str) -> FirmwareJob:
    return FirmwareJob(
        job_id="j1",
        configuration="kitchen.yaml",
        job_type=JobType.CLEAN,
        target_esphome_version=target_esphome_version,
    )


async def test_resolve_esphome_cmd_clean_never_provisions(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """CLEAN uses the cached venv only; it never pip-installs (cache miss → installed)."""
    controller = bare_firmware_controller_factory(
        esphome_cmd=["installed", "-m", "esphome"], with_mock_db=True
    )
    provisioner = controller._db.remote_build_receiver.state.env_provisioner
    provisioner.provision = AsyncMock()
    provisioner.cached_cmd = AsyncMock(return_value=None)  # not provisioned yet
    job = _clean_job("2026.5.0")

    assert await controller._resolve_esphome_cmd(job) == ["installed", "-m", "esphome"]
    provisioner.provision.assert_not_awaited()  # no build for a clean
    provisioner.cached_cmd.assert_awaited_once_with("2026.5.0")
    # The under-purge fallback is surfaced, not silent.
    assert any("may not fully purge" in line for line in job.output)


async def test_resolve_esphome_cmd_clean_uses_cached_venv(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """A CLEAN cleans under the build's venv when it's already provisioned (newer cleans more)."""
    controller = bare_firmware_controller_factory(
        esphome_cmd=["installed", "-m", "esphome"], with_mock_db=True
    )
    provisioner = controller._db.remote_build_receiver.state.env_provisioner
    provisioner.provision = AsyncMock()
    provisioner.cached_cmd = AsyncMock(return_value=["venv/bin/python", "-m", "esphome"])
    job = _clean_job("2026.5.0")

    cmd = await controller._resolve_esphome_cmd(job)

    assert cmd == ["venv/bin/python", "-m", "esphome"]
    provisioner.provision.assert_not_awaited()
    # A cached venv cleans fully, so no under-purge warning.
    assert not any("may not fully purge" in line for line in job.output)


async def test_resolve_esphome_cmd_installed_target_uses_installed(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """A target equal to the receiver's installed version needs no venv."""
    controller = bare_firmware_controller_factory(
        esphome_cmd=["installed", "-m", "esphome"], with_mock_db=True
    )
    provisioner = controller._db.remote_build_receiver.state.env_provisioner
    provisioner.provision = AsyncMock()

    cmd = await controller._resolve_esphome_cmd(_remote_job(_installed_esphome_version))

    assert cmd == ["installed", "-m", "esphome"]
    provisioner.provision.assert_not_awaited()


async def test_resolve_esphome_cmd_compile_no_provisioner_raises(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """A mismatched COMPILE with no reachable provisioner fails loud, never installed.

    Guards the shutdown-race hole: a remote COMPILE draining while ``stop()`` nulled
    the provisioner must not silently build with the wrong version.
    """
    controller = bare_firmware_controller_factory(
        esphome_cmd=["installed", "-m", "esphome"], with_mock_db=True
    )
    controller._db.remote_build_receiver = None

    with pytest.raises(EnvProvisionError):
        await controller._resolve_esphome_cmd(_remote_job("2026.5.0"))


async def test_resolve_esphome_cmd_clean_no_provisioner_uses_installed(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """A CLEAN with no provisioner is version-insensitive, so the installed esphome is fine."""
    controller = bare_firmware_controller_factory(
        esphome_cmd=["installed", "-m", "esphome"], with_mock_db=True
    )
    controller._db.remote_build_receiver = None
    job = _clean_job("2026.5.0")

    assert await controller._resolve_esphome_cmd(job) == ["installed", "-m", "esphome"]
    # No receiver to provision from, so the under-purge is still surfaced.
    assert any("may not fully purge" in line for line in job.output)


async def test_resolve_esphome_cmd_propagates_provision_error(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """A failed provision propagates so the job fails and the offloader re-routes."""
    controller = bare_firmware_controller_factory(
        esphome_cmd=["installed", "-m", "esphome"], with_mock_db=True
    )
    provisioner = controller._db.remote_build_receiver.state.env_provisioner
    provisioner.provision = AsyncMock(side_effect=EnvProvisionError("boom"))

    with pytest.raises(EnvProvisionError):
        await controller._resolve_esphome_cmd(_remote_job("2026.5.0"))
