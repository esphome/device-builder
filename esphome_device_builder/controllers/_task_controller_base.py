"""Base class for controllers that schedule fire-and-forget background work.

A bare :func:`asyncio.create_task` returns a weak reference: if no
caller holds the task, the event loop can drop it mid-await and the
GC reaps it. The standard remedy is "add to a set, schedule, set the
discard-on-done callback" — repeated verbatim across several
controllers. :class:`TaskControllerBase` provides that boilerplate
as a single-inheritance base; subclasses ``super().__init__()`` and
schedule via ``self._track_task(coro)`` instead of a bare
:func:`asyncio.create_task`.

Exposes:

* :class:`TaskControllerBase` — base providing the ``_tasks`` set
  and the ``_track_task`` helper.
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
