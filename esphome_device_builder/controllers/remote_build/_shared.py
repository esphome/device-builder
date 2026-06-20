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
import logging
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from ...helpers.storage import ShutdownCallback
from .._task_controller_base import TaskControllerBase

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.event_bus import Event, EventType

_LOGGER = logging.getLogger(__name__)


async def drain_tasks(tasks: Iterable[asyncio.Task[Any]], *, log_exceptions: bool = False) -> None:
    """Cancel and await every task in *tasks*, swallowing exceptions.

    With *log_exceptions*, a non-cancellation exception from a settled
    task is logged at WARNING (still not propagated) instead of being
    dropped silently. Snapshots *tasks* to a list so the caller's
    post-drain ``clear`` doesn't pull tasks out from under the gather.
    Caller owns clearing the source collection.
    """
    tasks_list = list(tasks)
    if not tasks_list:
        return
    for task in tasks_list:
        task.cancel()
    results = await asyncio.gather(*tasks_list, return_exceptions=True)
    if not log_exceptions:
        return
    for task, result in zip(tasks_list, results, strict=True):
        # ``CancelledError`` is a ``BaseException`` (not ``Exception``),
        # so the expected cancel outcome is skipped here.
        if isinstance(result, Exception):
            _LOGGER.warning("task %r failed during drain", task.get_name(), exc_info=result)


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
