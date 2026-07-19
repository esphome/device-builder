"""Offloader-side bundle phase: build the YAML bundle with live job-log output."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ...helpers.async_ import run_in_executor
from ...helpers.config_bundle import build_yaml_bundle
from .helpers import _ingest_notice_line, _ingest_output_line

if TYPE_CHECKING:
    from ...models.firmware import FirmwareJob
    from .controller import FirmwareController

_HEARTBEAT_INTERVAL_SECONDS = 30.0


async def run_bundle_phase(
    controller: FirmwareController, job: FirmwareJob, cancel_event: asyncio.Event
) -> bytes | None:
    """
    Build *job*'s YAML bundle, streaming subprocess output into the job log.

    Returns the bundle bytes, or ``None`` when *cancel_event* fired first
    (the caller finalises the cancel). ``FileNotFoundError`` /
    ``BundleBuildError`` from the build propagate. A heartbeat notice
    ticks while the bundle runs so a silent validator still shows liveness.
    """
    yaml_path = await run_in_executor(controller._db.settings.rel_path, job.configuration)
    bus = controller.bus
    _ingest_notice_line(job, bus, "building configuration bundle for remote build")
    loop = asyncio.get_running_loop()
    bundle_task = loop.create_task(
        build_yaml_bundle(yaml_path, on_output=lambda line: _ingest_output_line(job, bus, line))
    )
    cancel_wait = loop.create_task(cancel_event.wait())
    heartbeat = loop.create_task(_run_heartbeat(controller, job))
    try:
        await asyncio.wait({bundle_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        heartbeat.cancel()
        cancel_wait.cancel()
        if not bundle_task.done():
            bundle_task.cancel()
        await asyncio.gather(bundle_task, heartbeat, cancel_wait, return_exceptions=True)
    if bundle_task.cancelled():
        return None
    bundle_bytes = bundle_task.result()
    _ingest_notice_line(
        job, bus, f"bundle ready ({len(bundle_bytes) / 1024:.0f} KiB); sending to build server"
    )
    return bundle_bytes


async def _run_heartbeat(controller: FirmwareController, job: FirmwareJob) -> None:
    """Tick a synthetic still-building notice into the job log every interval."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    while True:
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        elapsed = round(loop.time() - started)
        _ingest_notice_line(job, controller.bus, f"still building bundle ({elapsed}s elapsed)")
