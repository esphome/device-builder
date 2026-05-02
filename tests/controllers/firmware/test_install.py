"""End-to-end coverage for ``FirmwareController.install``.

The handler itself is small — it forwards to ``_validate_port``,
``_validate_configuration_boundary``, ``_create_job`` and
``_enqueue``. Each piece is tested in isolation elsewhere
(``test_install_to_specific_address.py`` for port shapes,
``test_traversal_validation.py`` for configuration validation,
``test_rename_lock.py`` for lock handling). What was missing was
the wiring: that ``install`` actually composes those pieces with
the right defaults and order. This file pins:

- Happy path returns a queued ``FirmwareJob`` with
  ``JobType.INSTALL`` and the user-supplied port.
- ``port`` defaults to ``"OTA"`` (not the empty string the
  ``upload`` handler uses).
- A bad ``port`` is rejected before the (potentially expensive)
  configuration validation runs — so a typo with a missing config
  still names the port as the offending input.
- ``JOB_QUEUED`` fires with the new job after enqueue.
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

    The validator inside ``install`` calls ``rel_path``, which needs
    a real ``config_dir`` / ``absolute_config_dir``; everything else
    in the install path (queue, persistence, supersede check, bus)
    is stubbed so the test stays focused on the handler's wiring.
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
async def test_install_returns_queued_job_with_install_type(tmp_path: Path) -> None:
    """Happy path: handler returns a ``QUEUED`` ``FirmwareJob`` of type ``INSTALL``.

    The frontend keys its "live tasks" panel off the ``status`` and
    ``job_type`` fields; pin both so a future refactor that defaults
    to ``COMPILE`` (the most common job type) shows up immediately.
    """
    controller = _controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.status == JobStatus.QUEUED
    assert job.job_type == JobType.INSTALL
    assert job.configuration == "kitchen.yaml"


@pytest.mark.asyncio
async def test_install_defaults_port_to_ota(tmp_path: Path) -> None:
    """``port`` defaults to ``"OTA"``, not the empty ``upload`` default.

    The CLI treats ``"OTA"`` as a request to resolve the configured
    device's address from the YAML. The ``upload`` handler keeps
    the empty default for backward compat with the legacy spawn
    protocol; ``install`` defaults to ``"OTA"`` so the common case
    of "flash the device named in the YAML" doesn't need a port
    arg from the caller.
    """
    controller = _controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert job.port == "OTA"


@pytest.mark.parametrize(
    "port",
    ["/dev/ttyUSB0", "192.168.1.5", "kitchen.local", "fe80::1"],
)
@pytest.mark.asyncio
async def test_install_forwards_custom_port_to_job(tmp_path: Path, port: str) -> None:
    """Caller-supplied port shapes (serial / IP / hostname) round-trip onto the job.

    ``_build_command`` reads ``job.port`` to render the
    ``--device`` flag at compile time; if the handler dropped or
    mutated the value here, the install would silently re-target
    OTA instead of the user-named address.
    """
    controller = _controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml", port=port)

    assert job.port == port


@pytest.mark.asyncio
async def test_install_validates_port_before_configuration(tmp_path: Path) -> None:
    """A typo'd port raises before the configuration validator runs.

    ``_validate_port`` is the first line of the handler. Its check
    is sub-microsecond; the configuration validator wraps a real
    ``Path.resolve`` syscall through an executor. Putting port
    first means a request that's bad on both fronts surfaces the
    cheap-to-detect failure first — and the offending value named
    in the error message identifies the *port*, not the
    configuration.

    Pin the order with a configuration the boundary validator
    would actually reject (a traversal payload). A swap of the
    two checks would surface the configuration error
    ("Invalid configuration filename …") instead of the
    port-shape error, and this assertion catches it.
    """
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.install(configuration="../etc/passwd", port="not a port")

    assert exc.value.code == ErrorCode.INVALID_ARGS
    assert "not a port" in exc.value.message
    assert "Invalid configuration filename" not in exc.value.message


@pytest.mark.asyncio
async def test_install_rejects_traversal_configuration(tmp_path: Path) -> None:
    """A traversal-shaped configuration trips the boundary validator.

    Already covered for every install / compile / upload variant in
    ``test_traversal_validation.py``'s ``_validate_configuration_boundary``
    suite; pinning it here too because ``install`` is the busiest
    public entry point and a regression in this handler specifically
    would be felt by every "Update" button click.
    """
    controller = _controller(tmp_path)

    with pytest.raises(CommandError) as exc:
        await controller.install(configuration="../etc/passwd")

    assert exc.value.code == ErrorCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_install_enqueues_before_firing_job_queued(tmp_path: Path) -> None:
    """``_queue.put`` runs *before* the ``JOB_QUEUED`` broadcast.

    The all-jobs panel keys off ``JOB_QUEUED`` to add a row when a
    new job lands; without this event the panel goes silent until
    the first ``JOB_OUTPUT`` line arrives (sometimes a few seconds
    later for cold-start compiles).

    Ordering matters: ``_enqueue`` calls ``await self._queue.put``
    *before* firing the bus event. A frontend that receives
    ``JOB_QUEUED`` and immediately calls ``firmware/follow_job``
    races the runner — if the event broadcast preceded the queue
    insert, the follower could attach to a queue that hasn't seen
    the job yet, producing a dropped first line. Verify both
    halves: the event fires with the right payload, *and* the
    queue had already received the job by the time the event
    fired.

    Implementation: capture a single shared call log via a
    ``MagicMock`` whose ``method_calls`` is updated in put-then-
    fire order. The ``_queue`` and ``bus`` mocks installed in the
    fixture are wired as attribute children of this parent so
    every call (sync or async) lands on the parent's call log.
    """
    parent = MagicMock()
    parent.queue.put = AsyncMock()
    parent.bus.fire = MagicMock()

    controller = _controller(tmp_path)
    controller._queue = parent.queue
    controller._db.bus = parent.bus
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    method_names = [name for name, _, _ in parent.method_calls]
    queued_idx = method_names.index("queue.put")
    fired_idx = method_names.index("bus.fire")
    assert queued_idx < fired_idx

    parent.bus.fire.assert_any_call(EventType.JOB_QUEUED, {"job": job})


@pytest.mark.asyncio
async def test_install_registers_job_in_jobs_map(tmp_path: Path) -> None:
    """The new job lands in ``self._jobs`` keyed by ``job_id``.

    Subsequent ``firmware/get_jobs`` / ``firmware/cancel`` /
    ``firmware/follow_job`` calls all look the job up by id;
    forgetting to register it here would leave those handlers
    raising ``"Job not found"`` for a job the user just queued.
    """
    controller = _controller(tmp_path)
    (tmp_path / "kitchen.yaml").write_text("")

    job = await controller.install(configuration="kitchen.yaml")

    assert controller._jobs[job.job_id] is job
