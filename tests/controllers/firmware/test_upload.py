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
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.config import DashboardSettings
from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.models import ErrorCode, EventType, JobStatus, JobType


def _controller(tmp_path: Path) -> FirmwareController:
    """Build a controller wired to a real ``DashboardSettings`` for path validation.

    Mirrors ``test_install.py``'s helper — the validator inside
    ``upload`` calls ``rel_path``, which needs a real
    ``config_dir`` / ``absolute_config_dir``. Everything else
    (queue, persistence, supersede check, bus) is stubbed.
    """
    settings = DashboardSettings()
    settings.config_dir = tmp_path
    settings.absolute_config_dir = tmp_path.resolve()

    controller = FirmwareController.__new__(FirmwareController)
    controller._jobs = {}
    controller._queue = AsyncMock()
    controller._persist_jobs = AsyncMock()
    controller._supersede_active_jobs = AsyncMock()

    bus = MagicMock()
    bus.fire = MagicMock()
    controller._db = type("DB", (), {"settings": settings, "bus": bus})()
    return controller


@pytest.mark.asyncio
async def test_upload_returns_queued_job_with_upload_type(tmp_path: Path) -> None:
    """Happy path: handler returns a ``QUEUED`` ``FirmwareJob`` of type ``UPLOAD``.

    The frontend's "live tasks" panel keys off ``status`` and
    ``job_type`` to render a row; pinning ``UPLOAD`` here catches a
    refactor that defaults to ``COMPILE`` (the most common job
    type) by mistake.
    """
    controller = _controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.upload(configuration="kitchen.yaml")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.UPLOAD
    assert job.configuration == "kitchen.yaml"


@pytest.mark.asyncio
async def test_upload_defaults_port_to_empty_string(tmp_path: Path) -> None:
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
    controller = _controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.upload(configuration="kitchen.yaml")

    assert job.port == ""


@pytest.mark.parametrize(
    "port",
    ["OTA", "/dev/ttyUSB0", "192.168.1.5", "kitchen.local", "fe80::1"],
)
@pytest.mark.asyncio
async def test_upload_forwards_custom_port_to_job(tmp_path: Path, port: str) -> None:
    """Caller-supplied port shapes (OTA / serial / IP / hostname) round-trip onto the job.

    ``_build_command`` reads ``job.port`` to render the
    ``--device`` flag at compile time; if the handler dropped or
    mutated the value here, the upload would target the wrong
    device.
    """
    controller = _controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.upload(configuration="kitchen.yaml", port=port)

    assert job.port == port


@pytest.mark.asyncio
async def test_upload_validates_port_before_configuration(tmp_path: Path) -> None:
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
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.upload(configuration="../etc/passwd", port="not a port")

    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert "not a port" in exc.value.message
    assert "Invalid configuration filename" not in exc.value.message


@pytest.mark.asyncio
async def test_upload_rejects_traversal_configuration(tmp_path: Path) -> None:
    """A traversal-shaped configuration trips the boundary validator.

    The validator helper itself is fully covered in
    ``test_traversal_validation.py``; pinning the wiring here
    too because ``upload`` is one of the public WS entry points
    and a regression in this handler specifically would mean a
    direct WS client could path-traverse via ``configuration``
    even though every other handler stays gated.
    """
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.upload(configuration="../etc/passwd")

    assert exc.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_upload_enqueues_before_firing_job_queued(tmp_path: Path) -> None:
    """``_queue.put`` runs *before* the ``JOB_QUEUED`` broadcast.

    Same race-prevention contract as ``install``: a frontend
    that subscribes via ``firmware/follow_job`` on receipt of
    ``JOB_QUEUED`` would race the runner if the event broadcast
    preceded the queue insert — the follower could attach to a
    queue that hasn't seen the job yet, dropping the first line.

    Implementation: capture a single shared call log via a
    parent ``MagicMock`` whose ``method_calls`` is updated in
    put-then-fire order. The ``_queue`` and ``bus`` mocks
    installed in the fixture are wired as attribute children
    of this parent so every call (sync or async) lands on the
    parent's call log.
    """
    parent = MagicMock()
    parent.queue.put = AsyncMock()
    parent.bus.fire = MagicMock()

    controller = _controller(tmp_path)
    controller._queue = parent.queue
    controller._db.bus = parent.bus
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.upload(configuration="kitchen.yaml")

    method_names = [name for name, _, _ in parent.method_calls]
    queued_idx = method_names.index("queue.put")
    fired_idx = method_names.index("bus.fire")
    assert queued_idx < fired_idx

    parent.bus.fire.assert_any_call(EventType.JOB_QUEUED, {"job": job})


@pytest.mark.asyncio
async def test_upload_registers_job_in_jobs_map(tmp_path: Path) -> None:
    """The new job lands in ``self._jobs`` keyed by ``job_id``.

    Subsequent ``firmware/get_jobs`` / ``firmware/cancel`` /
    ``firmware/follow_job`` calls all look the job up by id;
    forgetting to register it here would leave those handlers
    raising ``"Job not found"`` for a job the user just queued.
    """
    controller = _controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.upload(configuration="kitchen.yaml")

    assert controller._jobs[job.job_id] is job
