"""Post-ack extract task registry for the ``submit_job`` receiver."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from functools import partial
from typing import Any

from ....helpers.async_ import drain_tasks, log_task_exit

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
        self._cancel_requested: set[_Key] = set()

    @property
    def active(self) -> bool:
        """Whether any extract task is live."""
        return bool(self._tasks)

    def spawn(self, key: _Key, coro: Coroutine[Any, Any, None]) -> None:
        """Run *coro* as the tracked extract task for *key*; refused once stopped."""
        if self.stopped:
            coro.close()
            return
        task = asyncio.create_task(coro, name=f"submit-job-extract-{key[1]}")
        self._tasks.add(task)
        self._index[key] = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(partial(self._drop_index, key))
        task.add_done_callback(partial(log_task_exit, f"submit-job extract {key[1]}"))

    def lock(self, dashboard_id: str) -> asyncio.Lock:
        """Per-peer FIFO lock serializing extracts."""
        if (lock := self._locks.get(dashboard_id)) is None:
            lock = self._locks[dashboard_id] = asyncio.Lock()
        return lock

    def is_tracked(self, key: _Key) -> bool:
        """Whether *key* has an extract task that has not reached the enqueue handoff."""
        return key in self._index

    def cancel(self, key: _Key) -> bool:
        """Flag *key* for cancellation; True when a live extract will honour it."""
        task = self._index.get(key)
        if task is None or task.done():
            return False
        self._cancel_requested.add(key)
        return True

    def consume(self, key: _Key) -> bool:
        """Discard and report any cancellation flag for *key*."""
        flagged = key in self._cancel_requested
        self._cancel_requested.discard(key)
        return flagged

    def handoff(self, key: _Key) -> None:
        """Drop *key* from the index; its queued job now resolves through the fan-out."""
        self._index.pop(key, None)

    async def stop(self) -> None:
        """Refuse new spawns, then cancel and drain every task."""
        self.stopped = True
        await drain_tasks(self._tasks)
        self._tasks.clear()
        self._locks.clear()

    def _drop_index(self, key: _Key, task: asyncio.Task[None]) -> None:
        """Done-callback backstop for tasks that never reached the enqueue handoff."""
        if self._index.get(key) is task:
            del self._index[key]
