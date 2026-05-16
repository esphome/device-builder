"""Single-worker per-device build-directory size refresher.

Owns the full build-size cache lifecycle so the
:class:`DevicesController` doesn't carry the bookkeeping for
yet another background job. The refresher exposes a tiny sync
``request(configuration)`` API for "this device probably needs a
fresh walk" and a ``start()`` / ``stop()`` pair for lifecycle.

Design constraints driving the class shape:

- **Heavy walks must serialize.** A typical compile dir is
  50 MB+ and a fleet of 50 devices on a backend cold-start would
  otherwise saturate disk I/O. One worker, one walk at a time —
  guaranteed by construction (a single ``while True:`` loop).

- **Bulk operations must coalesce.** "Clean N devices in a row"
  fires N ``JOB_COMPLETED`` events; if each spawned its own
  background task we'd pile up N coroutines all blocked on a
  shared lock. The pending queue is a ``set`` so repeated
  requests for the same configuration collapse to one slot,
  and there's no per-request task to pile up either.

- **Errors must not kill the worker.** A bad walk on one
  configuration logs and continues with the next; the worker
  sleeps on the wake event until the next request lands.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from ..helpers.build_size import (
    BuildDirSignal,
    BuildSizeRefreshResult,
    coerce_sidecar_int,
    find_stale_build_dirs,
    refresh_build_size_if_stale,
)
from ._wake_worker import WakeWorker

_LOGGER = logging.getLogger(__name__)

# Callback fired after a successful refresh actually changed the
# cached triple. The owner uses it to reload the device through
# the scanner so the in-memory ``Device.build_size_bytes`` picks
# up the freshly-persisted value via the metadata resolver. The
# return value is ignored — typed ``Awaitable[Any]`` so the
# scanner's existing ``async def reload(...) -> bool`` can be
# wired directly without a no-return adapter wrapper.
RefreshedCallback = Callable[[str], Awaitable[Any]]


class BuildSizeRefresher:
    """
    One persistent worker that drains build-size refresh requests serially.

    The owner pushes work via :meth:`request` (sync, side-effect-only)
    or :meth:`enqueue_stale_fleet` (async, runs the phase-A sweep
    and pushes any divergent configurations). The single worker
    task wakes whenever the pending set is non-empty, walks one
    device at a time, and notifies the owner via the
    ``on_refreshed`` callback after each successful change.
    """

    def __init__(
        self,
        get_filenames: Callable[[], Iterable[str]],
        get_metadata_snapshot: Callable[[], dict[str, dict[str, Any]]],
        persist_size: Callable[[str, BuildSizeRefreshResult], None],
        on_refreshed: RefreshedCallback,
    ) -> None:
        self._get_filenames = get_filenames
        self._get_metadata_snapshot = get_metadata_snapshot
        self._persist_size = persist_size
        self._on_refreshed = on_refreshed
        self._worker: WakeWorker[str] = WakeWorker()

    def request(self, configuration: str) -> None:
        """Queue a refresh and wake the worker.

        Worker drains ``set.pop()``-style so mid-walk requests
        for an already-running configuration land in the same
        set and get a fresh walk right after the current one
        finishes; duplicates coalesce.

        A subtlety worth knowing: if the worker is mid-walk
        inside ``loop.run_in_executor(None, ...)`` when ``stop()``
        fires, cancelling the task only abandons the *await* — the
        underlying executor thread keeps running until the walk
        finishes (Python doesn't expose a way to interrupt a
        running thread). ``DeviceBuilder.stop()`` calls
        ``loop.shutdown_default_executor()`` which waits on the
        residual thread, so process shutdown still drains cleanly.
        """
        self._worker.request(configuration)

    def start(self) -> None:
        """Spawn the worker task. Idempotent."""
        self._worker.start(self._run, name="BuildSizeRefresher")

    async def stop(self) -> None:
        """Cancel the worker and wait for it to exit cleanly."""
        await self._worker.stop()

    async def enqueue_stale_fleet(self) -> None:
        """
        Phase-A fleet sweep: find stale configurations and queue them.

        One executor job stats every configured device's build
        dir + freshness pair against the cached pair in the
        per-device metadata store snapshot taken on the loop
        side. Each divergent configuration gets pushed into the
        worker queue via :meth:`request`; the worker drains the
        queue on its own. Used on controller start to pick up
        CLI-compile drift across the whole catalog without
        saturating disk I/O on the cold path.
        """
        loop = asyncio.get_running_loop()
        filenames = list(self._get_filenames())
        if not filenames:
            return
        metadata = self._get_metadata_snapshot()
        stale = await loop.run_in_executor(None, find_stale_build_dirs, filenames, metadata)
        for configuration in stale:
            self.request(configuration)

    async def _run(self) -> None:
        """Phase-A sweep on startup, then drain the pending set on every wake."""
        loop = asyncio.get_running_loop()
        try:
            await self.enqueue_stale_fleet()
        except Exception:
            _LOGGER.exception("Initial build-size fleet sweep failed")
        while True:
            async with self._worker.drain():
                # One snapshot per drain cycle, not per item, so the
                # per-device cached-signal lookup is O(1) on a hash
                # rather than O(N) on a fresh fleet-wide copy.
                metadata = self._get_metadata_snapshot()
                while self._worker.pending:
                    configuration = self._worker.pending.pop()
                    entry = metadata.get(configuration, {})
                    cached = BuildDirSignal(
                        dir_mtime=coerce_sidecar_int(entry.get("build_size_dir_mtime")),
                        info_mtime=coerce_sidecar_int(entry.get("build_size_info_mtime")),
                    )
                    try:
                        result = await loop.run_in_executor(
                            None,
                            refresh_build_size_if_stale,
                            configuration,
                            cached,
                        )
                    except Exception:
                        _LOGGER.exception("Build-size refresh failed for %s", configuration)
                        continue
                    if result is None:
                        continue  # cache hit / no artifacts — nothing to publish
                    self._persist_size(configuration, result)
                    # Reflect the persisted signal into the local
                    # snapshot so a re-queue of the same configuration
                    # within this drain cycle sees the fresh cache.
                    metadata[configuration] = {
                        **metadata.get(configuration, {}),
                        "build_size_bytes": result.size_bytes,
                        "build_size_dir_mtime": result.signal.dir_mtime,
                        "build_size_info_mtime": result.signal.info_mtime,
                    }
                    try:
                        await self._on_refreshed(configuration)
                    except Exception:
                        _LOGGER.exception("on_refreshed callback failed for %s", configuration)
