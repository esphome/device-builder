"""Pending-set + wake-event + task lifecycle for background workers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

_LOGGER = logging.getLogger(__name__)


class WakeWorker[T]:
    """Sync-request + asyncio.Event-driven background worker scaffold.

    The base owns the pending set, the wake event, and the
    start/stop lifecycle. Owners wrap their per-iteration work in
    ``async with worker.drain():`` so :meth:`wait_idle` can await
    a full drain cycle without polling internal state.
    """

    def __init__(self) -> None:
        self.pending: set[T] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Set when the worker is parked with pending empty;
        # cleared by ``request`` so callers can ``await wait_idle``
        # to block until their request has actually been processed.
        self._idle = asyncio.Event()
        self._idle.set()

    def request(self, item: T) -> None:
        """Push *item* onto :attr:`pending` and wake the loop."""
        self.pending.add(item)
        self._idle.clear()
        self._wake.set()

    def start(
        self,
        run: Callable[[], Coroutine[Any, Any, None]],
        *,
        name: str | None = None,
    ) -> None:
        """Spawn the worker. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(run(), name=name or "WakeWorker")

    async def stop(self) -> None:
        """Cancel and await the worker. Idempotent.

        Sets the idle event on the way out so any
        :meth:`wait_idle` waiter parked through shutdown resumes
        rather than hanging.
        """
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

    @asynccontextmanager
    async def drain(self) -> AsyncIterator[None]:
        """Park until a request lands; mark idle when the body exits.

        Pairs the wake-receive with the idle-set in one structured
        block so the run loop cannot leave :meth:`wait_idle` hung.
        """
        await self._wake.wait()
        self._wake.clear()
        try:
            yield
        finally:
            if not self.pending:
                self._idle.set()

    async def wait_idle(self) -> None:
        """Park until pending is empty and no drain is in progress."""
        await self._idle.wait()
