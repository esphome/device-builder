"""Post-ack extract task registry for the ``submit_job`` receiver."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from functools import partial
from typing import Any

from ...helpers.async_ import drain_tasks, log_task_exit

_Key = tuple[str, str]


class ExtractWindow:
    """
    Post-ack extract tasks: per-peer FIFO serialization, cancel flags, drain.

    Keys are ``(dashboard_id, remote job_id)``; a task stays in the index
    until the enqueue handoff makes its job resolvable through the fan-out.
    """

    def __init__(self) -> None:
        self.stopped = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._index: dict[_Key, asyncio.Task[None]] = {}
        self._cancels: set[_Key] = set()

    def spawn(self, key: _Key, coro: Coroutine[Any, Any, None]) -> None:
        """Run *coro* as the tracked extract task for *key*."""
        task = asyncio.create_task(coro, name=f"submit-job-extract-{key[1]}")
        self._tasks.add(task)
        self._index[key] = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(partial(self._drop_index, key))
        task.add_done_callback(partial(log_task_exit, f"submit-job extract {key[1]}"))

    def lock(self, dashboard_id: str) -> asyncio.Lock:
        """Per-peer FIFO lock serializing extracts."""
        return self._locks.setdefault(dashboard_id, asyncio.Lock())

    def cancel(self, key: _Key) -> bool:
        """Flag *key* for cancellation; True when a live extract will honour it."""
        task = self._index.get(key)
        if task is None or task.done():
            return False
        self._cancels.add(key)
        return True

    def cancelled(self, key: _Key) -> bool:
        """Whether *key* has been flagged for cancellation."""
        return key in self._cancels

    def handoff(self, key: _Key) -> bool:
        """Drop *key* from the index (its job is queued); True when it was flagged."""
        self._index.pop(key, None)
        return key in self._cancels

    def clear_flag(self, key: _Key) -> None:
        """Drop any cancellation flag for *key*."""
        self._cancels.discard(key)

    async def stop(self) -> None:
        """Refuse new spawns, then cancel and drain every task, including mid-drain arrivals."""
        self.stopped = True
        while self._tasks:
            tasks = list(self._tasks)
            self._tasks.difference_update(tasks)
            await drain_tasks(tasks)
        self._locks.clear()
        self._index.clear()
        self._cancels.clear()

    def _drop_index(self, key: _Key, task: asyncio.Task[None]) -> None:
        """Done-callback backstop for tasks that never reached the enqueue handoff."""
        if self._index.get(key) is task:
            del self._index[key]
