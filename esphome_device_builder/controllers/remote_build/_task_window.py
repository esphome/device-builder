"""Tracked-task window for the receiver's off-loop workers."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from functools import partial
from typing import Any

from ...helpers.async_ import drain_tasks, log_task_exit


class TaskWindow:
    """Tracked task set with a stop gate: track, drain, refuse-after-stop."""

    def __init__(self) -> None:
        self.stopped = False
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def active(self) -> bool:
        """Whether any tracked task is live."""
        return bool(self._tasks)

    def track(self, coro: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None] | None:
        """Run *coro* as a tracked task; refused (``None``) once stopped."""
        if self.stopped:
            coro.close()
            return None
        # Deliberately lazy (not ``create_logged_task``): callers
        # register bookkeeping against the returned task before its
        # first segment runs — an eager start lets an extract outrun
        # its cancel registration.
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(partial(log_task_exit, name))
        return task

    async def wait_idle(self) -> None:
        """Await every live task without cancelling (test quiesce seam)."""
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self) -> None:
        """Refuse new tracks, then cancel and drain every task."""
        self.stopped = True
        await drain_tasks(self._tasks)
        self._tasks.clear()
