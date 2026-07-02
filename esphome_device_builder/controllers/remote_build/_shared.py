"""Shared scaffolding for the offloader and receiver siblings.

Exposes :class:`_RemoteBuildBase` — base class providing the
``_db`` / ``_listeners`` / ``_shutdown_callbacks`` fields on top of
:class:`TaskControllerBase`'s task scheduler. The per-role ``stop``
methods drain their task iterables via :func:`helpers.async_.drain_tasks`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from ...helpers.storage import ShutdownCallback
from .._task_controller_base import TaskControllerBase

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.event_bus import Event, EventType


class _RemoteBuildBase(TaskControllerBase):
    """Base for the offloader and receiver siblings.

    Subclasses call ``super().__init__(device_builder)`` to
    populate the fields, layer role-specific state on top, and
    define their own ``start`` / ``stop``. The role's ``stop``
    is responsible for closing :attr:`_listeners`, walking
    :attr:`_shutdown_callbacks`, and draining :attr:`_tasks`
    (via :func:`helpers.async_.drain_tasks`).
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        super().__init__()
        self._db = device_builder
        self._listeners = ExitStack()
        self._shutdown_callbacks: list[ShutdownCallback] = []

    def _subscribe(self, event_type: EventType, listener: Callable[[Event[Any]], None]) -> None:
        """Register *listener* for *event_type*, auto-removed when ``_listeners`` closes."""
        self._listeners.callback(self._db.bus.add_listener(event_type, listener))
