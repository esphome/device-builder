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
  sleeps on ``self._wake`` until the next request lands.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from ..helpers.build_size import find_stale_build_dirs, refresh_build_size_if_stale

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
        config_dir: Path,
        get_filenames: Callable[[], Iterable[str]],
        on_refreshed: RefreshedCallback,
    ) -> None:
        self._config_dir = config_dir
        self._get_filenames = get_filenames
        self._on_refreshed = on_refreshed
        self._pending: set[str] = set()
        self._wake = asyncio.Event()
        self._worker_task: asyncio.Task[None] | None = None

    def request(self, configuration: str) -> None:
        """
        Queue a refresh and wake the worker.

        Cheap, sync, side-effect-only — safe to call from any
        event-loop callback. Repeated requests for the same
        configuration coalesce because the queue is a ``set``,
        and a refresh that's already queued doesn't add a second
        slot. The worker picks the request up on its next
        iteration; mid-walk requests for an already-running
        configuration land in the same set and get a fresh walk
        right after the current one finishes.
        """
        self._pending.add(configuration)
        self._wake.set()

    def start(self) -> None:
        """Spawn the worker task. Idempotent — a second call is a no-op."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        """Cancel the worker and wait for it to exit cleanly.

        ``CancelledError`` is the expected exit and gets
        suppressed silently. Anything else is unexpected — a
        per-iteration ``except`` in the worker missed something —
        and gets logged so the failure isn't invisible during a
        clean controller shutdown.

        A subtlety worth knowing: if the worker is mid-walk
        inside ``loop.run_in_executor(None, ...)`` when ``stop()``
        fires, cancelling the task only abandons the *await* — the
        underlying executor thread keeps running until the walk
        finishes (Python doesn't expose a way to interrupt a
        running thread). For build-size refresh the work is just
        a directory walk + one sidecar write per device, so the
        thread completes on its own within a few seconds at
        worst; ``DeviceBuilder.stop()`` then calls
        ``loop.shutdown_default_executor()`` which waits on the
        residual thread, so process shutdown still drains
        cleanly. We accept this over a bespoke cancellation
        channel into the walk, because process shutdown is the
        only ``stop()`` caller and the trailing write is harmless.
        """
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Build-size worker failed during shutdown")
        self._worker_task = None

    async def enqueue_stale_fleet(self) -> None:
        """
        Phase-A fleet sweep: find stale configurations and queue them.

        One executor job stats every configured device's build
        dir + freshness pair against the cached pair in the
        sidecar and returns the divergent set. Each one gets
        pushed into the worker queue via :meth:`request`; the
        worker drains the queue on its own. Used on controller
        start to pick up CLI-compile drift across the whole
        catalog without saturating disk I/O on the cold path.
        """
        loop = asyncio.get_running_loop()
        filenames = list(self._get_filenames())
        if not filenames:
            return
        stale = await loop.run_in_executor(None, find_stale_build_dirs, self._config_dir, filenames)
        for configuration in stale:
            self.request(configuration)

    async def _worker(self) -> None:
        """
        Long-lived loop: drain the pending set when woken.

        On first iteration, runs the phase-A fleet sweep so the
        backend picks up CLI-compile drift across the whole
        catalog without the controller having to fire a separate
        startup task. Subsequent iterations sleep on ``self._wake``
        until there's work, clear the event, then walk the queue
        one configuration at a time until it's empty, then sleep
        again. ``set.pop()`` is arbitrary-order — fine here, every
        queued device gets walked. New requests that arrive
        mid-iteration land in the same set and get processed
        before the next sleep.

        Per-iteration ``try`` swallows configuration-specific
        errors so one bad walk can't kill the worker (typical
        cause: a permission error or a vanishing path during the
        walk). Cancellation propagates through the ``await`` and
        breaks the outer ``while True:`` cleanly.
        """
        loop = asyncio.get_running_loop()
        try:
            await self.enqueue_stale_fleet()
        except Exception:
            _LOGGER.exception("Initial build-size fleet sweep failed")
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._pending:
                configuration = self._pending.pop()
                try:
                    result = await loop.run_in_executor(
                        None,
                        refresh_build_size_if_stale,
                        self._config_dir,
                        configuration,
                    )
                except Exception:
                    _LOGGER.exception("Build-size refresh failed for %s", configuration)
                    continue
                if result is None:
                    continue  # cache hit / no artifacts — nothing to publish
                try:
                    await self._on_refreshed(configuration)
                except Exception:
                    _LOGGER.exception("on_refreshed callback failed for %s", configuration)
