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
        """Push *item* onto :attr:`pending` and wake the loop.

        Requires :meth:`start` to be in effect for progress;
        otherwise :meth:`wait_idle` will park until ``stop``.
        """
        self.pending.add(item)
        self._idle.clear()
        self._wake.set()

    def start(self) -> None:
        """Spawn the worker. Idempotent."""
        prior = self._task
        if prior is not None and not prior.done():
            return
        if prior is not None and not prior.cancelled():
            # Retrieve any unhandled exception so it doesn't GC as
            # "Task exception was never retrieved."
            exc = prior.exception()
            if exc is not None:
                _LOGGER.error("Worker %s crashed; restarting", prior.get_name(), exc_info=exc)
        # ``wait_idle`` right after ``start`` parks past ``_on_start``.
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

        A raise leaves whatever is still in :attr:`pending` for
        the next cycle — :meth:`_drain_cycle` re-arms the wake.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        try:
            await self._on_start()
        except Exception:
            _LOGGER.exception("Worker %s _on_start raised; continuing", type(self).__name__)
        # Re-set the idle ``start`` cleared if ``_on_start`` queued no work.
        if not self.pending:
            self._idle.set()
        while True:
            async with self._drain_cycle():
                try:
                    await self._drain()
                except Exception:
                    _LOGGER.exception("Worker %s drain raised; continuing", type(self).__name__)

    @asynccontextmanager
    async def _drain_cycle(self) -> AsyncIterator[None]:
        """Wait for a wake; mark idle on empty exit, re-arm wake on non-empty."""
        await self._wake.wait()
        self._wake.clear()
        try:
            yield
        finally:
            # Re-arm wake on non-empty so a ``_drain`` that raises
            # mid-pending isn't stranded; ``Event.set()`` is idempotent.
            if not self.pending:
                self._idle.set()
            else:
                self._wake.set()
