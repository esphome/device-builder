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
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode, EventType, JobStatus, JobType
from tests.controllers.firmware.conftest import FirmwareControllerFactory


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_compile_enqueues_before_firing_job_queued(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``_queue.put`` runs *before* the ``JOB_QUEUED`` broadcast.

    Same race-prevention contract as ``install`` and ``upload``:
    a frontend that subscribes via ``firmware/follow_job`` on
    receipt of ``JOB_QUEUED`` would race the runner if the
    event broadcast preceded the queue insert — the follower
    could attach to a queue that hasn't seen the job yet,
    dropping the first line.

    Implementation: capture a single shared call log via a
    parent ``MagicMock`` whose ``method_calls`` is updated in
    put-then-fire order. The ``_queue`` and ``bus`` mocks
    installed in the fixture are wired as attribute children of
    this parent so every call (sync or async) lands on the
    parent's call log.
    """
    parent = MagicMock()
    parent.queue.put = AsyncMock()
    parent.bus.fire = MagicMock()

    controller = firmware_controller_factory(with_queue=True)
    controller._queue = parent.queue
    controller._db.bus = parent.bus
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.compile(configuration="kitchen.yaml")

    method_names = [name for name, _, _ in parent.method_calls]
    queued_idx = method_names.index("queue.put")
    fired_idx = method_names.index("bus.fire")
    assert queued_idx < fired_idx

    parent.bus.fire.assert_any_call(EventType.JOB_QUEUED, {"job": job})


@pytest.mark.asyncio
async def test_compile_registers_job_in_jobs_map(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """The new job lands in ``self._jobs`` keyed by ``job_id``.

    Subsequent ``firmware/get_jobs`` / ``firmware/cancel`` /
    ``firmware/follow_job`` calls all look the job up by id;
    forgetting to register it here would leave those handlers
    raising ``"Job not found"`` for a job the user just queued.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.compile(configuration="kitchen.yaml")

    assert controller._jobs[job.job_id] is job
