"""Asyncio task helpers."""

from __future__ import annotations

from asyncio import AbstractEventLoop, Task, get_running_loop
from collections.abc import Coroutine
from typing import Any


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
