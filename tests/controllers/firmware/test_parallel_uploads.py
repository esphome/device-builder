"""Parallel-upload lanes: 3 concurrent normal flashes, OpenThread serialized.

Drives the real lane workers (via ``_run_queue``) against parking
subprocesses, mirroring ``test_lane_concurrency_e2e``. Pins the
esphome discussion #3781 shape: up to ``MAX_CONCURRENT_UPLOADS``
network flashes overlap so a slow OTA doesn't block fast ones, while
flashes of OpenThread devices — one shared mesh / border router —
stay one-at-a-time on their own lane.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from esphome_device_builder.controllers.firmware import controller as controller_module
from esphome_device_builder.controllers.firmware._state import FirmwareState
from esphome_device_builder.controllers.firmware.constants import MAX_CONCURRENT_UPLOADS
from esphome_device_builder.models import EventType, FirmwareJob, JobStatus, JobType
from tests.controllers.firmware.conftest import (
    wire_devices as _wire_devices,
)
from tests.controllers.firmware.conftest import (
    wire_real_queue as _wire_real_queue,
)

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory


# Every upload parks (prints a line, then blocks) until terminated.
_PARK_UPLOAD = "import sys, time\nprint('INFO uploading', flush=True)\ntime.sleep(30)\n"


def _watch_started(controller: Any) -> dict[str, asyncio.Event]:
    """Per-configuration JOB_STARTED events, auto-created on first fire."""
    started: dict[str, asyncio.Event] = {}
    bus = controller._db.bus
    real_fire = bus.fire

    def _capture(event_type: EventType, data: dict) -> None:
        real_fire(event_type, data)
        job = data.get("job") if isinstance(data, dict) else None
        if job is not None and event_type is EventType.JOB_STARTED:
            started.setdefault(job.configuration, asyncio.Event()).set()

    bus.fire = _capture
    return started


def _seed_yamls(tmp_path: Any, *names: str) -> None:
    for name in names:
        stem = name.removesuffix(".yaml")
        (tmp_path / name).write_text(f"esphome:\n  name: {stem}\n", encoding="utf-8")


async def _wait_started(started: dict[str, asyncio.Event], *configurations: str) -> None:
    async with asyncio.timeout(10):
        for configuration in configurations:
            await started.setdefault(configuration, asyncio.Event()).wait()


async def test_three_uploads_overlap_and_fourth_queues(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Any
) -> None:
    """Three flashes occupy the upload lane at once; the fourth waits for a slot."""
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller)
    controller.state.esphome_cmd = [sys.executable, "-c", _PARK_UPLOAD]
    names = ["u1.yaml", "u2.yaml", "u3.yaml", "u4.yaml"]
    _seed_yamls(tmp_path, *names)
    started = _watch_started(controller)

    jobs = [await controller.upload(configuration=name, port="OTA") for name in names]

    runner = asyncio.create_task(controller._run_queue())
    try:
        await _wait_started(started, *names[:MAX_CONCURRENT_UPLOADS])

        lane = controller.state.upload_lane
        assert len(lane.active) == MAX_CONCURRENT_UPLOADS
        assert all(j.status is JobStatus.RUNNING for j in jobs[:MAX_CONCURRENT_UPLOADS])
        assert jobs[3].status is JobStatus.QUEUED
        assert lane.queue.qsize() == 1
        status = controller.lane_status(lane)
        assert status.running is True
        assert status.idle is False

        # Freeing one slot lets the fourth in.
        controller.state.cancel_requested.add(jobs[0].job_id)
        await controller._terminate_job_process(jobs[0])
        await _wait_started(started, names[3])
        assert jobs[3].status is JobStatus.RUNNING
        assert jobs[0].status is JobStatus.CANCELLED
    finally:
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner


async def test_thread_uploads_serialize_beside_concurrent_normal_uploads(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Any
) -> None:
    """Two thread-device flashes take the thread lane one at a time; a normal flash overlaps."""
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller)
    controller._db.devices.thread_configurations = {"mesh1.yaml", "mesh2.yaml"}
    controller.state.is_thread_configuration = controller._is_thread_configuration
    controller.state.esphome_cmd = [sys.executable, "-c", _PARK_UPLOAD]
    _seed_yamls(tmp_path, "mesh1.yaml", "mesh2.yaml", "alpha.yaml")
    started = _watch_started(controller)

    mesh1 = await controller.upload(configuration="mesh1.yaml", port="OTA")
    mesh2 = await controller.upload(configuration="mesh2.yaml", port="OTA")
    alpha = await controller.upload(configuration="alpha.yaml", port="OTA")

    runner = asyncio.create_task(controller._run_queue())
    try:
        await _wait_started(started, "mesh1.yaml", "alpha.yaml")

        thread_lane = controller.state.thread_upload_lane
        assert list(thread_lane.active) == [mesh1.job_id]
        assert mesh2.status is JobStatus.QUEUED
        assert thread_lane.queue.qsize() == 1
        # The normal flash overlapped the thread flash on its own lane.
        assert alpha.status is JobStatus.RUNNING
        assert alpha.job_id in controller.state.upload_lane.active

        # The thread lane frees serially: mesh2 starts only after mesh1 ends.
        controller.state.cancel_requested.add(mesh1.job_id)
        await controller._terminate_job_process(mesh1)
        await _wait_started(started, "mesh2.yaml")
        assert list(thread_lane.active) == [mesh2.job_id]
    finally:
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner


async def test_cancel_one_of_three_concurrent_uploads_spares_siblings(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Any
) -> None:
    """Cancelling one running upload signals only its own subprocess."""
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller)
    controller.state.esphome_cmd = [sys.executable, "-c", _PARK_UPLOAD]
    names = ["u1.yaml", "u2.yaml", "u3.yaml"]
    _seed_yamls(tmp_path, *names)
    started = _watch_started(controller)

    jobs = [await controller.upload(configuration=name, port="OTA") for name in names]

    runner = asyncio.create_task(controller._run_queue())
    try:
        await _wait_started(started, *names)
        # JOB_STARTED fires before the spawn; wait for every registration.
        async with asyncio.timeout(10):
            while not all(job.job_id in controller.state.processes for job in jobs):
                await asyncio.sleep(0.01)

        await controller.cancel(job_id=jobs[1].job_id)
        async with asyncio.timeout(10):
            while jobs[1].status is not JobStatus.CANCELLED:
                await asyncio.sleep(0.01)

        # Siblings never saw a signal — still parked and RUNNING.
        assert jobs[0].status is JobStatus.RUNNING
        assert jobs[2].status is JobStatus.RUNNING
        assert controller.state.processes[jobs[0].job_id].returncode is None
        assert controller.state.processes[jobs[2].job_id].returncode is None
    finally:
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner


# ---------------------------------------------------------------------------
# Routing units — lane_for / place_on_lane / start() wiring
# ---------------------------------------------------------------------------


def _upload(configuration: str) -> FirmwareJob:
    return FirmwareJob(
        job_id=f"j-{configuration}", configuration=configuration, job_type=JobType.UPLOAD
    )


def test_lane_for_routes_thread_flashes_to_the_thread_lane() -> None:
    state = FirmwareState()
    state.is_thread_configuration = lambda c: c == "mesh.yaml"

    assert state.lane_for(_upload("mesh.yaml")) is state.thread_upload_lane
    assert state.lane_for(_upload("kitchen.yaml")) is state.upload_lane
    compile_job = FirmwareJob(job_id="c", configuration="mesh.yaml", job_type=JobType.COMPILE)
    assert state.lane_for(compile_job) is state.compile_lane


def test_lane_for_rename_tail_routes_on_the_old_name() -> None:
    state = FirmwareState()
    state.is_thread_configuration = lambda c: c == "old.yaml"
    tail = FirmwareJob(
        job_id="tail",
        configuration="old.yaml",
        job_type=JobType.RENAME,
        depends_on="head",
        new_name="fresh",
    )

    assert state.lane_for(tail) is state.thread_upload_lane


def test_place_on_lane_enqueues_thread_flash_on_the_thread_queue() -> None:
    """The restore / release_dependents router lands thread flashes on their lane."""
    state = FirmwareState()
    state.is_thread_configuration = lambda c: c == "mesh.yaml"

    state.place_on_lane(_upload("mesh.yaml"))
    state.place_on_lane(_upload("kitchen.yaml"))

    assert state.thread_upload_lane.queue.qsize() == 1
    assert state.upload_lane.queue.qsize() == 1


def test_upload_lane_concurrency_defaults() -> None:
    state = FirmwareState()
    assert state.upload_lane.max_concurrency == MAX_CONCURRENT_UPLOADS
    assert state.thread_upload_lane.max_concurrency == 1
    assert state.compile_lane.max_concurrency == 1
    assert state.thread_upload_lane in state.lanes()


async def test_start_wires_thread_lookup_before_job_restore(
    firmware_controller_factory: FirmwareControllerFactory,
    monkeypatch: Any,
) -> None:
    """Restored flashes must already route through the devices lookup."""
    controller = firmware_controller_factory()
    monkeypatch.setattr(controller_module, "_find_esphome_cmd", lambda: ["esphome"])
    monkeypatch.setattr(
        controller_module, "_verify_esphome_importable", AsyncMock(return_value=(True, "ok"))
    )
    seen: list[Any] = []

    async def _load_jobs() -> None:
        seen.append(controller.state.is_thread_configuration)

    monkeypatch.setattr(controller, "_load_jobs", _load_jobs)

    await controller.start()

    assert seen == [controller._is_thread_configuration]


def test_is_thread_configuration_defaults_false_without_devices(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory()
    assert controller._is_thread_configuration("anything.yaml") is False
