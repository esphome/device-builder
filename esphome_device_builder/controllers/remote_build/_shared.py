"""Shared infrastructure for the offloader and receiver siblings.

The two sibling controllers
(:class:`~.offloader.OffloaderController`,
:class:`~.receiver.ReceiverController`) own disjoint state and
disjoint method sets but happen to need the same per-instance
lifecycle scaffolding:

* a :class:`DeviceBuilder` ref for bus access / settings lookup,
* a strong-ref set of spawned :class:`asyncio.Task` so a
  fire-and-forget coroutine isn't GC'd mid-await,
* an :class:`ExitStack` to accumulate bus-listener unsubscribers,
* a list of per-file :class:`Store` flush callbacks for the
  ``stop()`` walk.

Hoisting that scaffolding to a thin :class:`_RemoteBuildBase`
base class collapses the duplicated ``__init__`` lines and the
``_track_task`` method that both siblings carried verbatim. Each
sibling still defines its own ``start`` / ``stop`` /
role-specific state on top.

Single-inheritance, not a mixin: every type checker resolves
the four attributes through one MRO step, so the
type-disambiguation headaches that killed the earlier mixin
attempt don't recur.
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
    """Lifecycle scaffolding shared by the offloader and receiver siblings.

    Concrete subclasses
    (:class:`~.offloader.OffloaderController`,
    :class:`~.receiver.ReceiverController`) call
    ``super().__init__(device_builder)`` to populate the four
    fields below, then layer their own role-specific state on
    top and define their own ``start`` / ``stop`` methods.
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._tasks: set[asyncio.Task[None]] = set()
        # Bus-listener unsubscribers; the role's ``stop`` closes
        # the stack to detach all of them in one pass.
        self._listeners = ExitStack()
        # Per-file :class:`Store` flush callbacks; the role's
        # ``stop`` walks them to drain debounced writes before
        # the in-RAM dicts go away.
        self._shutdown_callbacks: list[ShutdownCallback] = []

    def _track_task(
        self, coro: Coroutine[Any, Any, None], *, name: str | None = None
    ) -> asyncio.Task[None]:
        """Schedule *coro* and hold a strong ref in :attr:`_tasks` until it settles.

        Distinct from :meth:`DeviceBuilder.create_background_task`
        — this set is drained separately by each role's ``stop``
        for ordered subsystem teardown.
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
