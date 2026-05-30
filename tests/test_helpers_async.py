"""Tests for ``helpers/async_.py`` — eager task creation."""

from __future__ import annotations

import asyncio

from esphome_device_builder.helpers.async_ import create_eager_task


async def test_eager_task_runs_synchronously_until_first_await() -> None:
    ran: list[str] = []

    async def coro() -> str:
        ran.append("before")
        await asyncio.sleep(0)
        ran.append("after")
        return "done"

    task = create_eager_task(coro())
    assert ran == ["before"]
    assert await task == "done"
    assert ran == ["before", "after"]


async def test_eager_task_completes_without_loop_turn() -> None:
    async def coro() -> int:
        return 42

    task = create_eager_task(coro())
    assert task.done()
    assert task.result() == 42


async def test_eager_task_sets_name() -> None:
    async def coro() -> None:
        return None

    task = create_eager_task(coro(), name="my-task")
    assert task.get_name() == "my-task"
    await task


async def test_eager_task_uses_explicit_loop() -> None:
    loop = asyncio.get_running_loop()

    async def coro() -> None:
        return None

    task = create_eager_task(coro(), loop=loop)
    assert task.get_loop() is loop
    await task
