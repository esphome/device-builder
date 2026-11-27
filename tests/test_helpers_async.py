"""Tests for ``helpers/async_.py`` — eager task creation."""

from __future__ import annotations

import asyncio
import logging

import pytest

from esphome_device_builder.helpers.async_ import (
    create_eager_task,
    create_logged_task,
    log_gather_failures,
    run_to_completion,
)


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


async def test_logged_task_starts_eagerly_and_names_the_task() -> None:
    ran: list[bool] = []

    async def coro() -> None:
        ran.append(True)

    task = create_logged_task(coro(), name="worker")
    assert ran == [True]
    assert task.get_name() == "worker"
    await task


async def test_logged_task_logs_a_crash(caplog: pytest.LogCaptureFixture) -> None:
    async def coro() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("simulated crash")

    target = "esphome_device_builder.helpers.async_"
    with caplog.at_level("ERROR", logger=target):
        task = create_logged_task(coro(), name="doomed worker")
        await asyncio.gather(task, return_exceptions=True)

    assert any("doomed worker crashed" in r.getMessage() for r in caplog.records)


async def test_logged_task_logs_a_crash_before_the_first_await(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def coro() -> None:
        raise RuntimeError("synchronous crash")

    target = "esphome_device_builder.helpers.async_"
    with caplog.at_level("ERROR", logger=target):
        # Completes inside create_eager_task; the logger attaches to an
        # already-settled task and must still fire.
        task = create_logged_task(coro(), name="instant casualty")
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

    assert any("instant casualty crashed" in r.getMessage() for r in caplog.records)


def test_log_gather_failures_logs_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    """An ``Exception`` result is logged at WARNING and siblings are still processed."""
    with caplog.at_level(logging.WARNING):
        log_gather_failures([ValueError("boom"), "ok"], "item failed")

    assert [r.getMessage() for r in caplog.records] == ["item failed"]


def test_log_gather_failures_reraises_base_exception() -> None:
    """A cancellation result is re-raised rather than swallowed."""
    with pytest.raises(asyncio.CancelledError):
        log_gather_failures([asyncio.CancelledError()], "item failed")


async def test_run_to_completion_finishes_after_the_caller_is_cancelled() -> None:
    release = asyncio.Event()
    finished: list[str] = []

    async def work() -> None:
        await release.wait()
        finished.append("done")

    caller = asyncio.create_task(run_to_completion(work(), name="work"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
    assert finished == ["done"]


async def test_run_to_completion_logs_a_failure_after_the_caller_is_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = asyncio.Event()

    async def work() -> None:
        await release.wait()
        raise RuntimeError("boom")

    caller = asyncio.create_task(run_to_completion(work(), name="late write"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    with caplog.at_level(logging.ERROR):
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
    assert "late write crashed: boom" in caplog.text


async def test_run_to_completion_returns_and_raises_like_the_coroutine() -> None:
    async def ok() -> int:
        await asyncio.sleep(0)
        return 7

    async def bad() -> None:
        raise ValueError("nope")

    assert await run_to_completion(ok(), name="ok") == 7
    with pytest.raises(ValueError, match="nope"):
        await run_to_completion(bad(), name="bad")
