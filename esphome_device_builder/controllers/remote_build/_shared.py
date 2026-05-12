"""Shared scaffolding for the offloader and receiver siblings.

Exposes:

* :class:`_RemoteBuildBase` — base class providing the
  ``_db`` / ``_tasks`` / ``_listeners`` /
  ``_shutdown_callbacks`` fields and the ``_track_task``
  helper both siblings need.
* :func:`drain_tasks` — stateless cancel-and-gather helper
  the per-role ``stop`` methods feed task iterables to.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterable
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from ...helpers.storage import ShutdownCallback

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder


async def drain_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
    """Cancel and await every task in *tasks*, swallowing exceptions.

    Snapshots *tasks* to a list so the caller's post-drain
    ``clear`` doesn't pull tasks out from under the gather.
    Caller owns clearing the source collection.
    """
    tasks_list = list(tasks)
    if not tasks_list:
        return
    for task in tasks_list:
        task.cancel()
    await asyncio.gather(*tasks_list, return_exceptions=True)


class _RemoteBuildBase:
    """Base for the offloader and receiver siblings.

    Subclasses call ``super().__init__(device_builder)`` to
    populate the four fields, layer role-specific state on top,
    and define their own ``start`` / ``stop``. The role's
    ``stop`` is responsible for closing :attr:`_listeners`,
    walking :attr:`_shutdown_callbacks`, and draining
    :attr:`_tasks` (via :func:`drain_tasks`).
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._tasks: set[asyncio.Task[None]] = set()
        self._listeners = ExitStack()
        self._shutdown_callbacks: list[ShutdownCallback] = []

    def _track_task(
        self, coro: Coroutine[Any, Any, None], *, name: str | None = None
    ) -> asyncio.Task[None]:
        """Schedule *coro* and hold a strong ref in :attr:`_tasks` until it settles.

        Distinct from :meth:`DeviceBuilder.create_background_task`:
        this set is drained separately by each role's ``stop``
        for ordered subsystem teardown.
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
