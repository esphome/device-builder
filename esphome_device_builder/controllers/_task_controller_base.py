"""Base class for controllers that schedule fire-and-forget background work.

The event loop keeps only a weak ref to each
:class:`~asyncio.Task`; an unreferenced task can be GC'd mid-await.
:class:`TaskControllerBase` provides the strong-ref set + schedule
helper so subclasses don't repeat the idiom inline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class TaskControllerBase:
    """Base for controllers that schedule fire-and-forget tasks.

    Subclasses call ``super().__init__()`` to initialise
    :attr:`_tasks`, then schedule via ``self._track_task(coro)``.
    Each subclass's teardown is responsible for draining
    :attr:`_tasks` (cancel + gather) before clearing the set.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def _track_task(
        self, coro: Coroutine[Any, Any, None], *, name: str | None = None
    ) -> asyncio.Task[None]:
        """Schedule *coro* and hold a strong ref in :attr:`_tasks` until it settles."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
