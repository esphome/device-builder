"""Shared scaffolding for the offloader and receiver siblings.

Exposes:

* :class:`_RemoteBuildBase` — base class providing the
  ``_db`` / ``_listeners`` / ``_shutdown_callbacks`` fields
  on top of :class:`TaskControllerBase`'s task scheduler.
* :func:`drain_tasks` — stateless cancel-and-gather helper
  the per-role ``stop`` methods feed task iterables to.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from ...helpers.storage import ShutdownCallback
from .._task_controller_base import TaskControllerBase

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.event_bus import Event, EventType


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


class _RemoteBuildBase(TaskControllerBase):
    """Base for the offloader and receiver siblings.

    Subclasses call ``super().__init__(device_builder)`` to
    populate the fields, layer role-specific state on top, and
    define their own ``start`` / ``stop``. The role's ``stop``
    is responsible for closing :attr:`_listeners`, walking
    :attr:`_shutdown_callbacks`, and draining :attr:`_tasks`
    (via :func:`drain_tasks`).
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        super().__init__()
        self._db = device_builder
        self._listeners = ExitStack()
        self._shutdown_callbacks: list[ShutdownCallback] = []

    def _subscribe(self, event_type: EventType, listener: Callable[[Event[Any]], None]) -> None:
        """Register *listener* for *event_type*, auto-removed when ``_listeners`` closes."""
        self._listeners.callback(self._db.bus.add_listener(event_type, listener))
