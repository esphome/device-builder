"""Parallel-upload lanes: 3 concurrent normal flashes, OpenThread serialized.

Drives the real lane workers (via ``_run_queue``) against parking
subprocesses, mirroring ``test_lane_concurrency_e2e``. Pins the
deep-sleep wake-delivery shape (esphome discussion #3781): several
woken devices' queued updates flash concurrently — up to
``MAX_CONCURRENT_UPLOADS`` — instead of missing their wake windows
behind one slow OTA, while flashes of OpenThread devices — one shared
mesh / border router — stay one-at-a-time on their own lane.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.controllers.firmware._state import FirmwareState
from esphome_device_builder.controllers.firmware.constants import MAX_CONCURRENT_UPLOADS
from esphome_device_builder.models import EventType, FirmwareJob, JobStatus, JobType
from tests.controllers.firmware.conftest import run_until_terminal, seed_yamls, wire_real_queue
from tests.controllers.firmware.conftest import wire_devices as _wire_devices

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


async def _wait_started(started: dict[str, asyncio.Event], *configurations: str) -> None:
    async with asyncio.timeout(10):
        for configuration in configurations:
            await started.setdefault(configuration, asyncio.Event()).wait()


async def test_three_uploads_overlap_and_fourth_queues(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Any
) -> None:
    """Three flashes occupy the upload lane at once; the fourth waits for a slot."""
    controller = firmware_controller_factory(with_queue=True)
    wire_real_queue(controller)
    _wire_devices(controller)
    controller.state.esphome_cmd = [sys.executable, "-c", _PARK_UPLOAD]
    names = ["u1.yaml", "u2.yaml", "u3.yaml", "u4.yaml"]
    seed_yamls(tmp_path, *names)
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
    wire_real_queue(controller)
    _wire_devices(controller)
    controller.state.is_thread_configuration = lambda c: c in {"mesh1.yaml", "mesh2.yaml"}
    controller.state.esphome_cmd = [sys.executable, "-c", _PARK_UPLOAD]
    seed_yamls(tmp_path, "mesh1.yaml", "mesh2.yaml", "alpha.yaml")
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
    wire_real_queue(controller)
    _wire_devices(controller)
    controller.state.esphome_cmd = [sys.executable, "-c", _PARK_UPLOAD]
    names = ["u1.yaml", "u2.yaml", "u3.yaml"]
    seed_yamls(tmp_path, *names)
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


# Each upload sleeps a per-device staggered duration, then exits 0 —
# a burst of woken deep-sleep devices whose OTAs take different times.
_STAGGERED_UPLOAD = (
    "import os, sys, time\n"
    "cfg = os.path.basename(next(a for a in sys.argv if a.endswith('.yaml')))\n"
    "delays = {'u1.yaml': 0.2, 'u2.yaml': 0.05, 'u3.yaml': 0.35,"
    " 'u4.yaml': 0.1, 'u5.yaml': 0.05, 'u6.yaml': 0.15}\n"
    "print('INFO uploading', flush=True)\n"
    "time.sleep(delays[cfg])\n"
)


async def test_six_uploads_drain_three_at_a_time(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Any
) -> None:
    """A 6-upload burst drains 3-at-a-time: overflow starts only as slots free.

    The deep-sleep wake shape: six devices wake at once, each flash
    finishing at its own staggered time. The lane must never exceed
    3 concurrent flashes, the first 3 must all start before anything
    finishes, and the k-th overflow upload starts only after k
    earlier flashes completed (FIFO refill). All 6 land COMPLETED.
    """
    controller = firmware_controller_factory(with_queue=True)
    wire_real_queue(controller)
    _wire_devices(controller)
    controller.state.esphome_cmd = [sys.executable, "-c", _STAGGERED_UPLOAD]
    names = [f"u{i}.yaml" for i in range(1, 7)]
    seed_yamls(tmp_path, *names)

    sequence: list[tuple[EventType, str]] = []
    max_active = 0
    bus = controller._db.bus
    real_fire = bus.fire

    def _record(event_type: EventType, data: dict) -> None:
        nonlocal max_active
        max_active = max(max_active, len(controller.state.upload_lane.active))
        job = data.get("job") if isinstance(data, dict) else None
        if job is not None:
            sequence.append((event_type, job.configuration))
        real_fire(event_type, data)

    bus.fire = _record

    jobs = [await controller.upload(configuration=name, port="OTA") for name in names]
    await run_until_terminal(controller)

    assert all(job.status is JobStatus.COMPLETED for job in jobs)
    assert max_active == MAX_CONCURRENT_UPLOADS

    completions_before_start: dict[str, int] = {}
    done = 0
    for event_type, configuration in sequence:
        if event_type is EventType.JOB_STARTED:
            completions_before_start[configuration] = done
        elif event_type is EventType.JOB_COMPLETED:
            done += 1
    # The first wave fills every slot before anything finishes...
    assert [completions_before_start[name] for name in names[:3]] == [0, 0, 0]
    # ...and each overflow upload waits for enough earlier flashes to land.
    for k, name in enumerate(names[3:], start=1):
        assert completions_before_start[name] >= k


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


def test_controller_wires_thread_lookup_at_construction() -> None:
    """Every route — including restore — sees the devices lookup, not the default."""
    controller = FirmwareController(MagicMock())
    assert controller.state.is_thread_configuration == controller._is_thread_configuration


def test_is_thread_configuration_defaults_false_without_devices(
    firmware_controller_factory: FirmwareControllerFactory,
    caplog: Any,
) -> None:
    """A missing devices controller degrades loudly, not silently."""
    controller = firmware_controller_factory()
    assert controller._is_thread_configuration("anything.yaml") is False
    assert any(
        "without thread serialization" in record.message
        for record in caplog.records
        if record.levelname == "WARNING"
    )


def test_is_thread_configuration_delegates_to_devices(
    firmware_controller_factory: FirmwareControllerFactory,
) -> None:
    controller = firmware_controller_factory()
    controller._db.devices = MagicMock(is_thread_device=lambda c: c == "mesh.yaml")
    assert controller._is_thread_configuration("mesh.yaml") is True
    assert controller._is_thread_configuration("kitchen.yaml") is False
