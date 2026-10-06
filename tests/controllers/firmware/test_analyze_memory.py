"""Coverage for ``FirmwareController.analyze_memory`` and its CLI argv."""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode, EventType, JobSource, JobStatus, JobType
from tests.controllers.firmware.conftest import (
    BareFirmwareControllerFactory,
    CaptureEnqueueOrderFactory,
    EnqueueStep,
    FirmwareControllerFactory,
)


async def test_analyze_memory_returns_queued_local_job(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Happy path: a ``QUEUED`` ``ANALYZE_MEMORY`` job that always builds locally."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.analyze_memory(configuration="kitchen.yaml")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.ANALYZE_MEMORY
    assert job.configuration == "kitchen.yaml"
    assert job.source == JobSource.LOCAL


async def test_analyze_memory_rejects_traversal_configuration(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A traversal-shaped configuration trips the boundary validator."""
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as exc:
        await controller.analyze_memory(configuration="../etc/passwd")

    assert exc.value.code == ErrorCode.INVALID_ARGS


async def test_analyze_memory_enqueues_before_firing_job_queued(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    capture_enqueue_order: CaptureEnqueueOrderFactory,
) -> None:
    """``_queue.put`` runs before the ``JOB_QUEUED`` broadcast, so no follower races the runner."""
    controller = firmware_controller_factory(with_queue=True)
    log = capture_enqueue_order(controller, EventType.JOB_QUEUED)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.analyze_memory(configuration="kitchen.yaml")

    assert log[0] == (EnqueueStep.PUT, job)
    assert log[1][0] is EnqueueStep.FIRE
    assert log[1][1].event_type == EventType.JOB_QUEUED
    assert log[1][1].data == {"job": job}


async def test_analyze_memory_registers_job_in_jobs_map(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The new job is registered so ``get_job`` finds it by ``job_id``."""
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.analyze_memory(configuration="kitchen.yaml")

    assert await controller.get_job(job_id=job.job_id) is job


async def test_analyze_memory_cancels_active_build_for_same_configuration(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """One active job per device: the analysis supersedes the device's build."""
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    active = await controller.compile(configuration="kitchen.yaml")

    await controller.analyze_memory(configuration="kitchen.yaml")

    assert active.status == JobStatus.CANCELLED


def test_build_command_for_analyze_memory(
    tmp_path: Path,
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> None:
    """``ANALYZE_MEMORY`` runs ``esphome --dashboard analyze-memory <config>``, port ignored."""
    controller = bare_firmware_controller_factory(esphome_cmd=["esphome"], with_mock_db=True)
    config = str(tmp_path / "kitchen.yaml")

    cmd = controller._build_command(JobType.ANALYZE_MEMORY, config, port="/dev/ttyUSB0")

    assert cmd == ["esphome", "--dashboard", "analyze-memory", config]
