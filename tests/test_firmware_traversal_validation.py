"""Tests that firmware WS handlers reject traversal payloads at the boundary.

The ``_create_job`` chokepoint and the ``ext_storage_path`` openers
in ``firmware/get_binaries`` / ``firmware/download`` both call
``settings.rel_path`` so a traversal-shaped ``configuration`` value
surfaces synchronously as ``CommandError(INVALID_ARGS)`` from the
WS dispatcher — instead of being accepted, queued, then materialising
later as a failed job. This test pins that contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode, JobType


def _controller(tmp_path: Path) -> FirmwareController:
    """Stub controller wired to a real ``DashboardSettings.rel_path``."""
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()

    controller = FirmwareController.__new__(FirmwareController)
    controller._jobs = {}
    controller._db = type("DB", (), {"settings": settings})()
    return controller


@pytest.mark.parametrize(
    "job_type",
    [JobType.COMPILE, JobType.UPLOAD, JobType.CLEAN, JobType.INSTALL, JobType.RENAME],
)
def test_create_job_rejects_traversal(tmp_path: Path, job_type: JobType) -> None:
    """Every queue-creating job type validates ``configuration`` at the boundary.

    Without this, ``compile`` / ``upload`` / ``clean`` / ``install`` /
    ``rename`` would each accept a traversal payload, queue a job for
    it, and only fail later inside ``_execute_job`` where the runner's
    generic ``except Exception`` records it as a failed job — so the
    WS reply would be a ``FirmwareJob`` instead of ``INVALID_ARGS``.
    """
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        controller._create_job("../etc/passwd", job_type)
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


def test_create_job_allows_empty_for_reset_build_env(tmp_path: Path) -> None:
    """``RESET_BUILD_ENV`` jobs use the empty string and skip the check.

    ``rel_path("")`` would resolve to the config dir itself — fine,
    but the ``if configuration`` guard documents the intent and
    avoids ever syscalling on the wipe path.
    """
    controller = _controller(tmp_path)

    job = controller._create_job("", JobType.RESET_BUILD_ENV)
    assert job.configuration == ""


@pytest.mark.asyncio
async def test_get_binaries_rejects_traversal(tmp_path: Path) -> None:
    """``firmware/get_binaries`` re-validates because it bypasses ``rel_path``.

    The handler reads ``ext_storage_path(configuration)`` which lands
    in ``<data_dir>/storage/<configuration>.json`` — outside the
    config dir — so ``rel_path`` is the only gate.
    """
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await controller.get_binaries(configuration="../../etc/passwd")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_download_rejects_traversal(tmp_path: Path) -> None:
    """``firmware/download`` re-validates for the same reason."""
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as excinfo:
        await controller.download(configuration="../etc/passwd", file="firmware.bin")
    assert excinfo.value.code == ErrorCode.INVALID_ARGS
