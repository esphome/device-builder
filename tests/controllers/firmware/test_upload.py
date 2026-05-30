"""End-to-end coverage for ``FirmwareController.upload``.

Same shape as ``test_install.py``: each piece ``upload`` calls is
covered in isolation elsewhere — ``_validate_port`` in
``test_install_to_specific_address.py``,
``_validate_configuration_boundary`` in
``test_traversal_validation.py``, ``_build_command`` for the
``UPLOAD`` job type also in the install-to-specific-address tests.
What was missing was the wiring: that ``upload`` composes those
pieces with the right defaults and order. This file pins:

- Happy path returns a ``QUEUED`` ``FirmwareJob`` of type
  ``UPLOAD`` carrying the user's configuration.
- ``port`` defaults to the empty string (legacy spawn-protocol
  contract — distinct from ``install``'s ``"OTA"`` default).
- Custom port shapes round-trip onto ``job.port``.
- ``_validate_port`` runs *before*
  ``_validate_configuration_boundary``.
- ``_queue.put`` runs *before* ``JOB_QUEUED`` broadcasts so a
  ``firmware/follow_job`` subscriber doesn't race the queue
  insert.
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


async def test_upload_returns_queued_job_with_upload_type(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Happy path: handler returns a ``QUEUED`` ``FirmwareJob`` of type ``UPLOAD``.

    The frontend's "live tasks" panel keys off ``status`` and
    ``job_type`` to render a row; pinning ``UPLOAD`` here catches a
    refactor that defaults to ``COMPILE`` (the most common job
    type) by mistake.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.upload(configuration="kitchen.yaml")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.UPLOAD
    assert job.configuration == "kitchen.yaml"


async def test_upload_defaults_port_to_empty_string(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """``port`` defaults to ``""`` — *not* ``"OTA"``.

    The legacy spawn protocol defaults ``upload`` with no port,
    which lets the CLI auto-detect (serial via the OS, OTA via
    the configured address). ``install`` defaults to ``"OTA"`` to
    short-circuit auto-detect for the common
    "flash the device named in the YAML" path; ``upload`` keeps
    the legacy contract for backward compat with HA's
    ``esphome-dashboard-api``. Pin both so a refactor that
    unifies the defaults breaks visibly here AND in the install
    suite.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.upload(configuration="kitchen.yaml")

    assert job.port == ""


@pytest.mark.parametrize(
    "port",
    ["OTA", "/dev/ttyUSB0", "192.168.1.5", "kitchen.local", "fe80::1"],
)
async def test_upload_forwards_custom_port_to_job(
    tmp_path: Path, port: str, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """Caller-supplied port shapes (OTA / serial / IP / hostname) round-trip onto the job.

    ``_build_command`` reads ``job.port`` to render the
    ``--device`` flag at compile time; if the handler dropped or
    mutated the value here, the upload would target the wrong
    device.
    """
    controller = firmware_controller_factory(with_queue=True)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.upload(configuration="kitchen.yaml", port=port)

    assert job.port == port


async def test_upload_validates_port_before_configuration(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A typo'd port raises before the configuration validator runs.

    ``_validate_port`` is the first line of the handler. Its
    check is sub-microsecond; the configuration validator wraps
    a real ``Path.resolve`` syscall through an executor. Putting
    port first means a request that's bad on both fronts
    surfaces the cheap-to-detect failure first — and the
    offending value named in the error message identifies the
    *port*, not the configuration.

    Pin the order with a configuration the boundary validator
    would actually reject (a traversal payload). A swap of the
    two checks would surface the configuration error
    ("Invalid configuration filename …") instead of the
    port-shape error.
    """
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as exc:
        await controller.upload(configuration="../etc/passwd", port="not a port")

    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert "not a port" in exc.value.message
    assert "Invalid configuration filename" not in exc.value.message


async def test_upload_rejects_traversal_configuration(
    tmp_path: Path, firmware_controller_factory: FirmwareControllerFactory
) -> None:
    """A traversal-shaped configuration trips the boundary validator.

    The validator helper itself is fully covered in
    ``test_traversal_validation.py``; pinning the wiring here
    too because ``upload`` is one of the public WS entry points
    and a regression in this handler specifically would mean a
    direct WS client could path-traverse via ``configuration``
    even though every other handler stays gated.
    """
    controller = firmware_controller_factory(with_queue=True)

    with pytest.raises(CommandError) as exc:
        await controller.upload(configuration="../etc/passwd")

    assert exc.value.code == ErrorCode.INVALID_ARGS


async def test_upload_enqueues_before_firing_job_queued(
    tmp_path: Path,
    firmware_controller_factory: FirmwareControllerFactory,
    capture_enqueue_order: CaptureEnqueueOrderFactory,
) -> None:
    """``_queue.put`` runs *before* the ``JOB_QUEUED`` broadcast.

    Same race-prevention contract as ``install``: a frontend
    that subscribes via ``firmware/follow_job`` on receipt of
    ``JOB_QUEUED`` would race the runner if the event broadcast
    preceded the queue insert — the follower could attach to a
    queue that hasn't seen the job yet, dropping the first line.
    """
    controller = firmware_controller_factory(with_queue=True)
    log = capture_enqueue_order(controller, EventType.JOB_QUEUED)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.upload(configuration="kitchen.yaml")

    assert log[0] == (EnqueueStep.PUT, job)
    assert log[1][0] is EnqueueStep.FIRE
    assert log[1][1].event_type == EventType.JOB_QUEUED
    assert log[1][1].data == {"job": job}


async def test_upload_registers_job_in_jobs_map(
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

    job = await controller.upload(configuration="kitchen.yaml")

    assert await controller.get_job(job_id=job.job_id) is job
