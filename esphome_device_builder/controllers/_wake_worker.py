"""Pending-set + wake-event + task lifecycle for background workers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

_LOGGER = logging.getLogger(__name__)


class WakeWorker[T]:
    """Sync-request + asyncio.Event-driven background worker scaffold.

    Owner writes the drain loop and calls :meth:`wait` to park
    between iterations. The base owns the pending set, the wake
    event, and the start/stop lifecycle.
    """

    def __init__(self) -> None:
        self.pending: set[T] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def request(self, item: T) -> None:
        """Push *item* onto :attr:`pending` and wake the loop."""
        self.pending.add(item)
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
        """Cancel and await the worker. Idempotent."""
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

    async def wait(self) -> None:
        """Park until the wake fires, then clear it."""
        await self._wake.wait()
        self._wake.clear()
