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


@pytest.mark.parametrize(
    "active_type",
    ["compile", "upload", "install", "rename"],
)
@pytest.mark.parametrize(
    "active_status",
    [JobStatus.QUEUED, JobStatus.RUNNING],
)
@pytest.mark.asyncio
async def test_clean_rejects_when_active_build_for_same_configuration(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    active_type: str,
    active_status: JobStatus,
) -> None:
    """``clean`` refuses to run while a build is in flight.

    Compile / upload / install / rename for the same configuration all block.
    Other firmware commands rely on the ``_enqueue`` supersede
    path to cancel-and-replace the running job — that's the right
    shape for "user wants to retry the compile" — but a clean
    wipes the build artifacts the running job is producing, so a
    quietly-cancelled build that the user didn't intend to abandon
    is the worse failure mode. Reject loudly with
    ``CommandError(INVALID_ARGS)`` so the frontend can surface a
    "wait for the build to finish" toast instead of silently
    superseding. Both ``QUEUED`` (waiting in the queue) and
    ``RUNNING`` (live) block — no point letting a clean overwrite
    a build that's about to start either.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    if active_type == "compile":
        active = await controller.compile(configuration="kitchen.yaml")
    elif active_type == "upload":
        active = await controller.upload(configuration="kitchen.yaml", port="/dev/ttyUSB0")
    elif active_type == "install":
        active = await controller.install(configuration="kitchen.yaml")
    else:
        active = await controller.rename(configuration="kitchen.yaml", new_name="bedroom")
    # Submission lands the job in ``QUEUED``; the ``RUNNING``
    # variant promotes it (same justified seam as
    # ``test_supersede.py``'s RUNNING-carryover test — there's no
    # public API for putting a job into RUNNING without spawning
    # a real ``esphome``).
    active.status = active_status

    with pytest.raises(CommandError) as excinfo:
        await controller.clean(configuration="kitchen.yaml")

    assert excinfo.value.code == ErrorCode.INVALID_ARGS
    # Predecessor is still in its original state — clean did NOT supersede it.
    assert active.status == active_status


@pytest.mark.asyncio
async def test_clean_succeeds_when_active_build_targets_different_configuration(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A different device's build doesn't block cleaning this one.

    Sibling devices have independent build directories, so a
    compile on ``kitchen.yaml`` shouldn't prevent a clean on
    ``bedroom.yaml``.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    (tmp_path / "bedroom.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    other = await controller.compile(configuration="kitchen.yaml")
    other.status = JobStatus.RUNNING

    job = await controller.clean(configuration="bedroom.yaml")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.CLEAN


@pytest.mark.asyncio
async def test_clean_supersedes_other_active_clean_on_same_configuration(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """Two cleans for the same device still supersede.

    Re-running clean is harmless (just deletes build files
    already cleaned), and the second click is the user's
    explicit intent. Only compile/upload/install/rename block.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True, with_terminate=True)
    first = await controller.clean(configuration="kitchen.yaml")
    first.status = JobStatus.RUNNING
    controller._current_job = first

    second = await controller.clean(configuration="kitchen.yaml")

    assert second.status == JobStatus.QUEUED
    assert second.job_type == JobType.CLEAN
    assert second.job_id != first.job_id


@pytest.mark.asyncio
async def test_clean_succeeds_after_terminal_active_build(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    """A completed/failed/cancelled build doesn't block — only in-flight does.

    Terminal jobs hang around in ``_jobs`` for the recent-jobs
    history; the rejection check must filter them out so a
    crashed compile doesn't permanently lock the device out of
    cleaning.
    """
    (tmp_path / "kitchen.yaml").write_text("")
    controller = firmware_controller_factory(with_queue=True)
    failed = await controller.compile(configuration="kitchen.yaml")
    failed.status = JobStatus.FAILED

    job = await controller.clean(configuration="kitchen.yaml")

    assert job.status == JobStatus.QUEUED
