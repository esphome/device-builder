"""End-to-end coverage for ``FirmwareController.clean``.

Same shape as ``test_compile.py`` — ``clean`` is the smallest
submission handler after ``compile``: no port, no rename target,
just configuration → queued ``CLEAN`` job. The pieces it calls
are covered in isolation elsewhere
(``_validate_configuration_boundary`` in
``test_traversal_validation.py``, ``_create_job`` / ``_enqueue``
lifecycles across the broader suite); this file pins the
wiring.

Pinning matters because ``clean`` and ``compile`` share an
identical control-flow shape, and a refactor that "unifies" the
two handlers is the obvious accident that would silently flip
``CLEAN`` to ``COMPILE`` (or vice versa) without any production
test catching it.
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


@pytest.mark.asyncio
async def test_clean_returns_queued_job_with_clean_type(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Happy path: handler returns a ``QUEUED`` ``FirmwareJob`` of type ``CLEAN``.

    The frontend's "live tasks" panel keys off ``status`` and
    ``job_type`` to render a row; pinning ``CLEAN`` here catches
    a refactor that defaults to ``COMPILE`` (the structurally
    identical neighbour — same handler shape, same control flow,
    just a different ``JobType`` constant).
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.clean(configuration="kitchen.yaml")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.CLEAN
    assert job.configuration == "kitchen.yaml"


@pytest.mark.asyncio
async def test_clean_rejects_traversal_configuration(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A traversal-shaped configuration trips the boundary validator.

    The validator helper itself is fully covered in
    ``test_traversal_validation.py``; pinning the wiring here
    too because every public WS submission handler needs the
    boundary gate, and a regression in this specific handler
    would mean a direct WS client could path-traverse via
    ``configuration`` even though every other submission
    handler stays gated.
    """
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as exc:
        await controller.clean(configuration="../etc/passwd")

    assert exc.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_clean_enqueues_before_firing_job_queued(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    capture_enqueue_order: CaptureEnqueueOrderFactory,
) -> None:
    """``_queue.put`` runs *before* the ``JOB_QUEUED`` broadcast.

    Same race-prevention contract every other submission
    handler pins: a frontend that subscribes via
    ``firmware/follow_job`` on receipt of ``JOB_QUEUED`` would
    race the runner if the event broadcast preceded the queue
    insert — the follower could attach to a queue that hasn't
    seen the job yet, dropping the first line.
    """
    controller = firmware_controller_factory(with_queue=True)
    log = capture_enqueue_order(controller, EventType.JOB_QUEUED)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.clean(configuration="kitchen.yaml")

    assert log[0] == (EnqueueStep.PUT, job)
    assert log[1][0] is EnqueueStep.FIRE
    assert log[1][1].event_type == EventType.JOB_QUEUED
    assert log[1][1].data == {"job": job}


@pytest.mark.asyncio
async def test_clean_registers_job_in_jobs_map(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """The new job is registered so ``get_job`` finds it by ``job_id``.

    Subsequent ``firmware/get_jobs`` / ``firmware/cancel`` /
    ``firmware/follow_job`` calls all look the job up by id;
    forgetting to register it here would leave those handlers
    raising ``"Job not found"`` for a job the user just queued.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.clean(configuration="kitchen.yaml")

    assert await controller.get_job(job_id=job.job_id) is job
