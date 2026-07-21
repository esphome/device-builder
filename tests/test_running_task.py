"""Tests for the shared ``running_task`` background-task context manager."""

from __future__ import annotations

import asyncio

import pytest

from .conftest import running_task


async def test_running_task_cancels_the_task_on_exit() -> None:
    """A still-running task is cancelled and drained when the block exits."""
    started = asyncio.Event()

    async def _forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    async with running_task(_forever()) as task:
        await started.wait()
        assert not task.done()

    assert task.cancelled()


async def test_running_task_reraises_a_background_crash() -> None:
    """A task that dies with a non-cancellation error surfaces instead of being swallowed."""

    async def _boom() -> None:
        raise RuntimeError("background boom")

    with pytest.raises(RuntimeError, match="background boom"):
        async with running_task(_boom()):
            await asyncio.sleep(0)  # let it crash before the block exits


async def test_running_task_does_not_mask_a_body_failure() -> None:
    """A failure inside the block wins over a concurrent background crash."""

    async def _boom() -> None:
        raise RuntimeError("background boom")

    with pytest.raises(AssertionError, match="body failed"):
        async with running_task(_boom()):
            await asyncio.sleep(0)
            raise AssertionError("body failed")
