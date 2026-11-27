"""Asyncio task helpers."""

from __future__ import annotations

import logging
from asyncio import (
    AbstractEventLoop,
    CancelledError,
    Task,
    gather,
    get_running_loop,
    wait,
)
from collections.abc import Callable, Coroutine, Iterable
from functools import partial
from traceback import format_tb
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Strong refs: the loop only weakly references a task a cancelled caller stopped awaiting.
_UNCANCELLABLE: set[Task[Any]] = set()


def log_task_exit(label: str, task: Task[Any]) -> None:
    """Done-callback surfacing an unexpected task crash instead of a silent death."""
    if task.cancelled():
        return
    if (exc := task.exception()) is not None:
        _LOGGER.error("%s crashed: %s", label, exc, exc_info=exc)


def log_gather_failures(results: Iterable[Any], message: str) -> None:
    """
    Triage a ``gather(..., return_exceptions=True)`` result list.

    Logs each failure at WARNING so one item's failure doesn't skip
    its siblings; re-raises a cancellation rather than masking it as
    a benign miss.
    """
    for result in results:
        if isinstance(result, Exception):
            _LOGGER.warning("%s", message, exc_info=result)
        elif isinstance(result, BaseException):
            raise result


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


async def run_in_executor[T](fn: Callable[..., T], *args: Any) -> T:
    """Run ``fn(*args)`` in the default executor thread pool.

    Thin wrapper over ``get_running_loop().run_in_executor(None, ...)`` so
    callers stop re-deriving the loop for the common block-shift-to-a-thread
    case. Pass a ``functools.partial`` / lambda when *fn* needs keywords.
    """
    return await get_running_loop().run_in_executor(None, fn, *args)


async def run_to_completion[T](coro: Coroutine[Any, Any, T], *, name: str) -> T:
    """
    Await *coro* in a task that finishes even if the caller is cancelled.

    A cancelled caller still gets ``CancelledError``; the task's later failure is logged
    without its message, which can carry config text.
    """
    task = create_eager_task(coro, name=name)
    _UNCANCELLABLE.add(task)
    task.add_done_callback(_UNCANCELLABLE.discard)
    try:
        # Not ``shield``: on 3.14 it logs an abandoned failure with its message.
        await wait((task,))
    except CancelledError:
        task.add_done_callback(partial(_log_abandoned_failure, name))
        raise
    return task.result()


async def drain_uncancellable(timeout: float) -> None:
    """Wait up to *timeout* seconds for every ``run_to_completion`` task to finish."""
    if pending := set(_UNCANCELLABLE):
        await wait(pending, timeout=timeout)


def create_logged_task[T](
    coro: Coroutine[Any, Any, T],
    *,
    name: str,
    loop: AbstractEventLoop | None = None,
) -> Task[T]:
    """Create a named eager task that logs an unexpected crash."""
    task = create_eager_task(coro, name=name, loop=loop)
    task.add_done_callback(partial(log_task_exit, name))
    return task


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


def _log_abandoned_failure(name: str, task: Task[Any]) -> None:
    """Log a failure nobody awaits by type and stack only; the message can hold secrets."""
    if task.cancelled() or (exc := task.exception()) is None:
        return
    stack = "".join(format_tb(exc.__traceback__))
    _LOGGER.error(
        "%s failed after its caller was cancelled: %s\n%s", name, type(exc).__name__, stack
    )
