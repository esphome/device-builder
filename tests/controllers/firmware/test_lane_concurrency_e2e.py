"""End-to-end proof that the compile and upload lanes run concurrently.

The whole point of the two-lane queue (esphome discussion #3702): a
network-bound upload must not block the next device's compile. This file
drives both real lane consumers (via ``_run_queue``) against real
subprocesses — an upload that parks in its subprocess on one device while
a compile for another device runs to completion on the compile lane —
and pins that both lanes were occupied at the same time. A regression
that re-serialised the queue (one shared worker) would leave the compile
QUEUED behind the parked upload and trip the timeout here.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from esphome_device_builder.models import EventType, JobStatus, JobType
from tests.controllers.firmware.conftest import (
    wire_devices as _wire_devices,
)
from tests.controllers.firmware.conftest import (
    wire_real_queue as _wire_real_queue,
)

if TYPE_CHECKING:
    from .conftest import FirmwareControllerFactory


# An upload that parks (prints a line, then blocks) so the compile lane has
# a window to run; the compile prints a line and exits 0 right away. One
# script, branched on the subcommand the runner splats into argv.
_PARK_UPLOAD_RUN_COMPILE = (
    "import sys, time\n"
    "if 'upload' in sys.argv:\n"
    "    print('INFO uploading', flush=True)\n"
    "    time.sleep(30)\n"
    "else:\n"
    "    print('INFO compiling', flush=True)\n"
    "    sys.exit(0)\n"
)


async def test_parked_upload_does_not_block_a_compile(
    firmware_controller_factory: FirmwareControllerFactory, tmp_path: Any
) -> None:
    """A compile completes while an upload on the other lane is still running (#3702)."""
    controller = firmware_controller_factory(with_queue=True)
    _wire_real_queue(controller)
    _wire_devices(controller)
    controller.state.esphome_cmd = [sys.executable, "-c", _PARK_UPLOAD_RUN_COMPILE]
    (tmp_path / "alpha.yaml").write_text("esphome:\n  name: alpha\n", encoding="utf-8")
    (tmp_path / "beta.yaml").write_text("esphome:\n  name: beta\n", encoding="utf-8")

    upload_started = asyncio.Event()
    compile_completed = asyncio.Event()
    upload_terminal = asyncio.Event()
    bus = controller._db.bus
    real_fire = bus.fire

    def _capture(event_type: EventType, data: dict) -> None:
        real_fire(event_type, data)
        job = data.get("job") if isinstance(data, dict) else None
        if job is None:
            return
        if event_type is EventType.JOB_STARTED and job.job_type is JobType.UPLOAD:
            upload_started.set()
        if event_type is EventType.JOB_COMPLETED and job.job_type is JobType.COMPILE:
            compile_completed.set()
        if job.job_type is JobType.UPLOAD and job.status in (
            JobStatus.CANCELLED,
            JobStatus.FAILED,
            JobStatus.COMPLETED,
        ):
            upload_terminal.set()

    bus.fire = _capture

    # OTA skips the serial chip-verify; the upload goes straight to its
    # (parking) subprocess on the upload lane.
    upload = await controller.upload(configuration="alpha.yaml", port="OTA")
    compile_job = await controller.compile(configuration="beta.yaml")

    runner = asyncio.create_task(controller._run_queue())
    try:
        async with asyncio.timeout(10):
            await asyncio.gather(upload_started.wait(), compile_completed.wait())

        # The headline assertion: the compile reached COMPLETED while the
        # upload is still RUNNING on the other lane — they overlapped.
        assert compile_job.status == JobStatus.COMPLETED
        assert upload.status == JobStatus.RUNNING
        assert controller.state.upload_lane.current_job is not None
        assert controller.state.upload_lane.current_job.job_id == upload.job_id
        # The compile lane freed itself the instant the compile finished,
        # ready for the next device rather than waiting on the upload.
        assert controller.state.compile_lane.current_job is None

        # Release the parked upload and let it unwind cleanly.
        controller.state.cancel_requested.add(upload.job_id)
        await controller._terminate_current_process(controller.state.upload_lane)
        async with asyncio.timeout(10):
            await upload_terminal.wait()
        assert upload.status == JobStatus.CANCELLED
    finally:
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner
