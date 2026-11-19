"""
RAM-canonical disk usage snapshot, refreshed in the background.

Seeded into ``subscribe_events``' ``initial_state`` and re-broadcast as
``disk_usage_updated``. A full walk runs shortly after start, after each
finished firmware job, after a device is removed (archive and delete wipe its
build) and after the remote-build cleanup sweep reclaims space; in between, a
periodic pass re-reads only the volumes' free space. If a walk fails before
any measurement exists, ``disk_usage_failed`` fires so clients stop waiting;
the next pass (a job, or the 15-minute timer) tries again.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from esphome.core import CORE

from ..helpers.async_ import drain_tasks, run_in_executor
from ..helpers.build_size import resolve_build_dir
from ..helpers.disk_usage import (
    ToolchainSizeCache,
    build_roots,
    measure_disk_usage,
    refresh_volume_space,
)
from ..models import DiskUsage, DiskUsageEventData, DiskUsageFailedEventData, EventType
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
        # The last pass failed with no snapshot to fall back on.
        self._failed = False
        self._toolchain_cache = ToolchainSizeCache()
        # Volumes the last full pass found builds on; free-space passes re-read these.
        self._volume_paths: tuple[Path, ...] = ()
        self._worker = _DiskUsageWorker(self._run_pass)
        self._timer_task: asyncio.Task[None] | None = None
        self._unsubs: list[Callable[[], None]] = []

    def snapshot(self) -> DiskUsage | None:
        """Return the last measurement, or ``None`` before the first walk finishes."""
        return self._snapshot

    def measure_failed(self) -> bool:
        """Return whether measuring failed and no earlier measurement exists."""
        return self._failed

    def initial_state(self) -> dict[str, Any]:
        """Return the ``initial_state`` keys: the measurement, the failure flag, or neither."""
        if self._snapshot is not None:
            return {"disk_usage": self._snapshot.to_dict()}
        if self._failed:
            return {"disk_usage_failed": True}
        return {}

    def request_measure(self) -> None:
        """Queue a full walk; for code that frees space outside a firmware job."""
        self._worker.request(_Pass.FULL)

    async def start(self) -> None:
        """Subscribe to the events that move category sizes and schedule the first walk."""
        self._unsubs = [
            self._db.bus.add_listener(event_type, self._on_sizes_changed)
            for event_type in (*TERMINAL_JOB_EVENTS, EventType.DEVICE_REMOVED)
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

    def _on_sizes_changed(self, _event: Any) -> None:
        self.request_measure()

    async def _run_timer(self) -> None:
        await asyncio.sleep(_INITIAL_DELAY_SECONDS)
        self._worker.request(_Pass.FULL)
        while True:
            await asyncio.sleep(_FREE_SPACE_INTERVAL_SECONDS)
            self._worker.request(_Pass.FREE_SPACE)

    async def _run_pass(self, passes: set[_Pass]) -> None:
        try:
            if _Pass.FULL in passes or self._snapshot is None:
                devices = self._db.devices
                filenames = [d.configuration for d in devices.get_devices()] if devices else []
                usage, self._volume_paths = await run_in_executor(self._measure, filenames)
            else:
                usage = await run_in_executor(
                    refresh_volume_space, self._snapshot, self._volume_paths
                )
        except Exception:
            _LOGGER.exception("Disk usage pass %s failed", sorted(passes))
            # With a snapshot, clients keep showing it; without one they'd wait forever.
            if self._snapshot is None and not self._failed:
                self._failed = True
                self._db.bus.fire(EventType.DISK_USAGE_FAILED, DiskUsageFailedEventData())
            return
        self._failed = False
        if usage == self._snapshot:
            return
        self._snapshot = usage
        payload: DiskUsageEventData = {"disk_usage": usage}
        self._db.bus.fire(EventType.DISK_USAGE_UPDATED, payload)

    def _measure(self, filenames: list[str]) -> tuple[DiskUsage, tuple[Path, ...]]:
        """Executor side: ``CORE.data_dir`` and the StorageJSON build paths touch the disk."""
        data_dir = Path(CORE.data_dir).resolve()
        device_dirs = [d for name in filenames if (d := resolve_build_dir(name)) is not None]
        builds = build_roots(data_dir, device_dirs)
        usage = measure_disk_usage(
            self._db.settings.config_dir, data_dir, builds, self._toolchain_cache
        )
        return usage, (data_dir, *builds)


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
        await self._run_pass(passes)
