"""Uncancellable tasks for writes a cancelled caller must not interrupt."""

from __future__ import annotations

import logging
from asyncio import AbstractEventLoop, CancelledError, Task, current_task, get_running_loop, wait
from collections.abc import Coroutine
from functools import partial
from traceback import format_tb
from typing import Any
from weakref import WeakKeyDictionary

from ..models import ErrorCode
from .api import CommandError
from .async_ import create_eager_task

_LOGGER = logging.getLogger(__name__)

# Strong refs: the loop only weakly references a task a cancelled caller stopped awaiting.
_UNCANCELLABLE: set[Task[Any]] = set()
# A quiesced loop's shared drain deadline (``loop.time()``); its presence refuses new work.
_QUIESCED: WeakKeyDictionary[AbstractEventLoop, float] = WeakKeyDictionary()


async def run_to_completion[T](coro: Coroutine[Any, Any, T], *, name: str) -> T:
    """
    Await *coro* in a task that finishes even if the caller is cancelled.

    A cancelled caller still gets ``CancelledError``; the task's later failure is logged
    without its message, which can carry config text. On a quiesced loop new work is
    refused with ``UNAVAILABLE``; work nested in a running task still runs.
    """
    loop = get_running_loop()
    if loop in _QUIESCED and current_task() not in _UNCANCELLABLE:
        coro.close()
        raise CommandError(ErrorCode.UNAVAILABLE, "Shutting down; nothing was written")
    task = create_eager_task(_registered(coro), name=name, loop=loop)
    try:
        # Not ``shield``: on 3.14 it logs an abandoned failure with its message.
        await wait((task,))
    except CancelledError:
        task.add_done_callback(partial(_log_abandoned_failure, name))
        raise
    return task.result()


async def quiesce_uncancellable(timeout: float) -> None:
    """
    Refuse new top-level ``run_to_completion`` work, then wait for the tasks still running.

    Repeat calls on one loop share the first call's deadline of *timeout* seconds.
    """
    loop = get_running_loop()
    deadline = _QUIESCED.setdefault(loop, loop.time() + timeout)
    if pending := {t for t in _UNCANCELLABLE if t.get_loop() is loop and not t.done()}:
        await wait(pending, timeout=max(0.0, deadline - loop.time()))


def resume_uncancellable() -> None:
    """Let ``run_to_completion`` accept new work on this loop again after a quiesce."""
    _QUIESCED.pop(get_running_loop(), None)


async def _registered[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run *coro* as a registered task, so a call it nests at once counts as nested work."""
    task = current_task()
    assert task is not None  # type narrowing: always awaited inside its task
    _UNCANCELLABLE.add(task)
    try:
        return await coro
    finally:
        _UNCANCELLABLE.discard(task)


def _log_abandoned_failure(name: str, task: Task[Any]) -> None:
    """Log a failure nobody awaits without its message, which can hold secrets."""
    if task.cancelled() or (exc := task.exception()) is None:
        return
    if isinstance(exc, CommandError):
        # A refusal (a stale precondition, say) is an outcome, not a crash.
        _LOGGER.warning("%s was refused after its caller was cancelled: %s", name, exc.code)
        return
    stack = "".join(format_tb(exc.__traceback__))
    _LOGGER.error(
        "%s failed after its caller was cancelled: %s\n%s", name, type(exc).__name__, stack
    )
