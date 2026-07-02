"""Asyncio task helpers."""

from __future__ import annotations

import logging
from asyncio import AbstractEventLoop, Task, gather, get_running_loop
from collections.abc import Coroutine, Iterable
from typing import Any

_LOGGER = logging.getLogger(__name__)


async def drain_tasks(tasks: Iterable[Task[Any]], *, log_exceptions: bool = False) -> None:
    """
    Cancel and await every task in *tasks*, swallowing exceptions.

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
    results = await gather(*tasks_list, return_exceptions=True)
    if not log_exceptions:
        return
    for task, result in zip(tasks_list, results, strict=True):
        # ``CancelledError`` is a ``BaseException`` (not ``Exception``),
        # so the expected cancel outcome is skipped here.
        if isinstance(result, Exception):
            _LOGGER.warning("task %r failed during drain", task.get_name(), exc_info=result)


def create_eager_task[T](
    coro: Coroutine[Any, Any, T],
    *,
    name: str | None = None,
    loop: AbstractEventLoop | None = None,
) -> Task[T]:
    """
    Create a task from a coroutine and schedule it to run immediately.

    ``eager_start=True`` runs the coroutine synchronously up to its first
    suspension point, so one that completes without ever awaiting never
    reaches the event loop's task queue.
    """
    if loop is None:
        loop = get_running_loop()
    return Task(coro, loop=loop, name=name, eager_start=True)
