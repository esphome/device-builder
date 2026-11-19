"""
RAM-canonical disk usage snapshot, refreshed in the background.

Seeded into ``subscribe_events``' ``initial_state`` and re-broadcast as
``disk_usage_updated``. A full walk runs shortly after start and after each
finished firmware job; in between, a periodic pass re-reads only the
volume's free space, since the category sizes only move through jobs.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.core import CORE

from ..helpers.async_ import drain_tasks, run_in_executor
from ..helpers.disk_usage import ToolchainSizeCache, measure_disk_usage, refresh_volume_space
from ..models import DiskUsage, DiskUsageEventData, EventType
from ..models.firmware import TERMINAL_JOB_EVENTS
from ._wake_worker import WakeWorker

if TYPE_CHECKING:
    from collections.abc import Callable

    from esphome_device_builder.device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

# Holds the first walk out of the cold-start window.
_INITIAL_DELAY_SECONDS = 30.0
# Lets a burst of finished jobs (an install's compile + upload, a bulk
# update) coalesce into one walk.
_SETTLE_SECONDS = 5.0
_FREE_SPACE_INTERVAL_SECONDS = 15 * 60.0


class _Pass(StrEnum):
    FULL = "full"
    FREE_SPACE = "free_space"


class DiskUsageController:
    """Owns the disk usage snapshot and the worker that keeps it fresh."""

    def __init__(self, db: DeviceBuilder) -> None:
        self._db = db
        self._snapshot: DiskUsage | None = None
        self._toolchain_cache = ToolchainSizeCache()
        self._worker = _DiskUsageWorker(self._run_pass)
        self._timer_task: asyncio.Task[None] | None = None
        self._unsubs: list[Callable[[], None]] = []

    def snapshot(self) -> DiskUsage | None:
        """Return the last measurement, or ``None`` before the first walk finishes."""
        return self._snapshot

    async def start(self) -> None:
        """Subscribe to job completions and schedule the first walk."""
        self._unsubs = [
            self._db.bus.add_listener(event_type, self._on_job_finished)
            for event_type in TERMINAL_JOB_EVENTS
        ]
        self._worker.start()
        self._timer_task = asyncio.create_task(self._run_timer(), name="disk usage timer")

    async def stop(self) -> None:
        """Unsubscribe and stop the worker."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._timer_task is not None:
            self._timer_task.cancel()
            await drain_tasks((self._timer_task,))
            self._timer_task = None
        await self._worker.stop()

    def _on_job_finished(self, _event: Any) -> None:
        self._worker.request(_Pass.FULL)

    async def _run_timer(self) -> None:
        await asyncio.sleep(_INITIAL_DELAY_SECONDS)
        self._worker.request(_Pass.FULL)
        while True:
            await asyncio.sleep(_FREE_SPACE_INTERVAL_SECONDS)
            self._worker.request(_Pass.FREE_SPACE)

    async def _run_pass(self, passes: set[_Pass]) -> None:
        config_dir = self._db.settings.config_dir
        data_dir = Path(CORE.data_dir)
        if _Pass.FULL in passes or self._snapshot is None:
            usage = await run_in_executor(
                measure_disk_usage, config_dir, data_dir, self._toolchain_cache
            )
        else:
            usage = await run_in_executor(refresh_volume_space, self._snapshot, data_dir)
        if usage == self._snapshot:
            return
        self._snapshot = usage
        payload: DiskUsageEventData = {"disk_usage": usage}
        self._db.bus.fire(EventType.DISK_USAGE_UPDATED, payload)


class _DiskUsageWorker(WakeWorker[_Pass]):
    """Serializes passes; requests landing during the settle window share one pass."""

    def __init__(self, run_pass: Callable[[set[_Pass]], Any]) -> None:
        super().__init__()
        self._run_pass = run_pass

    async def _drain(self) -> None:
        await asyncio.sleep(_SETTLE_SECONDS)
        # Pop rather than swap_pending: a job finishing mid-walk must queue another pass.
        passes = set(self.pending)
        self.pending.clear()
        try:
            await self._run_pass(passes)
        except Exception:
            _LOGGER.exception("Disk usage pass %s failed", sorted(passes))
