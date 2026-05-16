"""Pending-set + wake-event + task lifecycle for background workers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_LOGGER = logging.getLogger(__name__)


class WakeWorker[T]:
    """Sync-request + asyncio.Event-driven background worker base.

    Subclasses implement :meth:`_drain` (called per wake) and
    optionally :meth:`_on_start` (one-shot, before the loop).
    The base owns the pending set, the wake event, the idle event,
    the start/stop lifecycle, and the drain context manager that
    pairs a wake-receive with an idle-set on exit.
    """

    def __init__(self) -> None:
        self.pending: set[T] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    def request(self, item: T) -> None:
        """Push *item* onto :attr:`pending` and wake the loop."""
        self.pending.add(item)
        self._idle.clear()
        self._wake.set()

    def start(self) -> None:
        """Spawn the worker. Idempotent."""
        prior = self._task
        if prior is not None and not prior.done():
            return
        if prior is not None and not prior.cancelled():
            # Retrieve any unhandled exception so it doesn't surface
            # as "Task exception was never retrieved" at GC time and
            # so the failure mode is visible in logs.
            exc = prior.exception()
            if exc is not None:
                _LOGGER.error("Worker %s crashed; restarting", prior.get_name(), exc_info=exc)
        # Clear idle so a ``wait_idle`` issued right after ``start``
        # parks until at least ``_on_start`` + the first drain
        # finishes. ``_run_loop`` re-sets it when ``_on_start``
        # queues no work.
        self._idle.clear()
        self._task = asyncio.create_task(self._run_loop(), name=type(self).__name__)

    async def stop(self) -> None:
        """Cancel and await the worker; unblock any :meth:`wait_idle` waiter."""
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Worker %s failed during shutdown", task.get_name())
        self._task = None
        self._idle.set()

    async def wait_idle(self) -> None:
        """Park until pending is empty and no drain is in progress."""
        await self._idle.wait()

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    async def _on_start(self) -> None:
        """One-shot hook called before the drain loop; default no-op."""

    async def _drain(self) -> None:
        """Process the current pending set. Subclasses must override.

        Two patterns are supported:

        * **Swap-empty**: ``pending, self.pending = self.pending,
          set()`` once at the top, then iterate the local copy
          with per-item ``try`` / ``except``. ``DeviceScanner``
          uses this.
        * **Pop-as-you-go**: ``while self.pending:
          item = self.pending.pop()``. Lets mid-drain ``request``
          calls land in the same cycle. ``BuildSizeRefresher``
          uses this.

        Either way, the base does not require ``_drain`` to
        complete: a raise leaves whatever is still in
        ``self.pending`` for the next cycle, and
        :meth:`_drain_cycle` re-arms ``_wake`` so the worker
        comes back instead of deadlocking.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        await self._on_start()
        # ``start`` cleared idle so ``wait_idle`` parks past
        # ``_on_start``; if the hook queued no work, re-set so
        # waiters return without an artificial first-drain wait.
        if not self.pending:
            self._idle.set()
        while True:
            async with self._drain_cycle():
                try:
                    await self._drain()
                except Exception:
                    # Unexpected raise from a subclass ``_drain``
                    # body. Log and continue so a programming bug
                    # in one drain iteration doesn't kill the
                    # worker — that would leave every ``wait_idle``
                    # waiter parked forever until ``stop`` runs.
                    _LOGGER.exception(
                        "Worker %s drain raised; continuing",
                        type(self).__name__,
                    )

    @asynccontextmanager
    async def _drain_cycle(self) -> AsyncIterator[None]:
        """Wait for a wake; settle idle/wake on exit based on pending.

        Re-arms ``_wake`` when items remain so the next iteration
        still drains them — without this a ``_drain`` that raises
        mid-pending would deadlock (``_wake`` was cleared on
        entry, never re-armed, and any ``wait_idle`` waiter would
        be stranded). ``Event.set()`` is idempotent so the normal
        case (concurrent ``request`` already set ``_wake``) is a
        no-op.
        """
        await self._wake.wait()
        self._wake.clear()
        try:
            yield
        finally:
            if not self.pending:
                self._idle.set()
            else:
                self._wake.set()
