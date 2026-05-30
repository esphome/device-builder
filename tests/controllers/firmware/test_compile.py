"""End-to-end coverage for ``FirmwareController.compile``.

Same shape as ``test_install.py`` and ``test_upload.py``: each
piece the handler calls is covered in isolation elsewhere
(``_validate_configuration_boundary`` in
``test_traversal_validation.py``, ``_create_job`` /
``_enqueue`` lifecycles in the broader controller suite). What
was missing was the wiring — that ``compile`` actually composes
those pieces with the right job type and event ordering.

``compile`` is the smallest of the three submission handlers:
no port argument, no rename target, just configuration → queued
``COMPILE`` job. So this file is correspondingly slim — it pins
the four contract points where a refactor regression would
silently degrade the dashboard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode, EventType, JobStatus, JobType
from tests.controllers.firmware.conftest import (
    CaptureEnqueueOrderFactory,
    EnqueueStep,
    FirmwareControllerFactory,
)


async def test_compile_returns_queued_job_with_compile_type(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Happy path: handler returns a ``QUEUED`` ``FirmwareJob`` of type ``COMPILE``.

    The frontend's "live tasks" panel keys off ``status`` and
    ``job_type`` to render a row; pinning ``COMPILE`` here
    catches a refactor that defaults to a different job type
    (``INSTALL`` is the obvious accident since the install
    handler delegates through the same queue).
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.compile(configuration="kitchen.yaml")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.COMPILE
    assert job.configuration == "kitchen.yaml"


async def test_compile_rejects_traversal_configuration(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A traversal-shaped configuration trips the boundary validator.

    The validator helper itself is fully covered in
    ``test_traversal_validation.py``; pinning the wiring here
    too because ``compile`` is the lowest-friction public WS
    entry point (no port, no extra args) and a regression in
    this handler specifically would mean a direct WS client
    could path-traverse via ``configuration`` even though every
    other handler stays gated.
    """
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as exc:
        await controller.compile(configuration="../etc/passwd")

    assert exc.value.code == ErrorCode.INVALID_ARGS


async def test_compile_enqueues_before_firing_job_queued(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    capture_enqueue_order: CaptureEnqueueOrderFactory,
) -> None:
    """``_queue.put`` runs *before* the ``JOB_QUEUED`` broadcast.

    Same race-prevention contract as ``install`` and ``upload``:
    a frontend that subscribes via ``firmware/follow_job`` on
    receipt of ``JOB_QUEUED`` would race the runner if the
    event broadcast preceded the queue insert — the follower
    could attach to a queue that hasn't seen the job yet,
    dropping the first line.
    """
    controller = firmware_controller_factory(with_queue=True)
    log = capture_enqueue_order(controller, EventType.JOB_QUEUED)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.compile(configuration="kitchen.yaml")

    assert log[0] == (EnqueueStep.PUT, job)
    assert log[1][0] is EnqueueStep.FIRE
    assert log[1][1].event_type == EventType.JOB_QUEUED
    assert log[1][1].data == {"job": job}


async def test_compile_registers_job_in_jobs_map(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The new job is registered so ``get_job`` finds it by ``job_id``.

    Subsequent ``firmware/get_jobs`` / ``firmware/cancel`` /
    ``firmware/follow_job`` calls all look the job up by id;
    forgetting to register it here would leave those handlers
    raising ``"Job not found"`` for a job the user just queued.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.compile(configuration="kitchen.yaml")

    assert await controller.get_job(job_id=job.job_id) is job
