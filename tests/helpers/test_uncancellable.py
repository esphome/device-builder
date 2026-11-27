"""Tests for ``helpers/uncancellable.py``."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from esphome_device_builder.helpers import uncancellable
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.uncancellable import (
    quiesce_uncancellable,
    resume_uncancellable,
    run_to_completion,
)
from esphome_device_builder.models import ErrorCode


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

    secret = "hunter" + "2"

    async def work() -> None:
        await release.wait()
        raise RuntimeError(secret)

    caller = asyncio.create_task(run_to_completion(work(), name="late write"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    with caplog.at_level(logging.ERROR):
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
    assert "late write failed after its caller was cancelled: RuntimeError" in caplog.text
    assert secret not in caplog.text


async def test_run_to_completion_logs_an_abandoned_refusal_by_code_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = asyncio.Event()
    secret = "hunter" + "2"

    async def work() -> None:
        await release.wait()
        raise CommandError(ErrorCode.PRECONDITION_FAILED, secret)

    caller = asyncio.create_task(run_to_completion(work(), name="late save"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    with caplog.at_level(logging.WARNING):
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)

    assert "late save was refused after its caller was cancelled" in caplog.text
    assert secret not in caplog.text
    assert "Traceback" not in caplog.text


async def test_run_to_completion_returns_and_raises_like_the_coroutine() -> None:
    async def ok() -> int:
        await asyncio.sleep(0)
        return 7

    async def bad() -> None:
        raise ValueError("nope")

    assert await run_to_completion(ok(), name="ok") == 7
    with pytest.raises(ValueError, match="nope"):
        await run_to_completion(bad(), name="bad")


async def test_quiesce_waits_for_a_task_its_caller_abandoned() -> None:
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
    asyncio.get_running_loop().call_soon(release.set)

    await quiesce_uncancellable(timeout=5)

    assert finished == ["done"]


async def test_quiesce_returns_at_once_when_nothing_runs() -> None:
    await asyncio.wait_for(quiesce_uncancellable(timeout=5), timeout=1)


async def test_quiesce_refuses_new_top_level_work() -> None:
    ran: list[str] = []

    async def work() -> None:
        ran.append("ran")

    await quiesce_uncancellable(timeout=5)

    with pytest.raises(CommandError) as err:
        await run_to_completion(work(), name="late")
    assert err.value.code == ErrorCode.UNAVAILABLE
    assert ran == []


async def test_quiesce_lets_work_nested_in_a_running_task_through() -> None:
    go = asyncio.Event()
    ran: list[str] = []

    async def inner() -> None:
        ran.append("inner")

    async def outer() -> None:
        await go.wait()
        await run_to_completion(inner(), name="inner")

    caller = asyncio.create_task(run_to_completion(outer(), name="outer"))
    await asyncio.sleep(0)
    quiesce = asyncio.create_task(quiesce_uncancellable(timeout=5))
    await asyncio.sleep(0)
    go.set()

    await asyncio.wait_for(quiesce, timeout=5)
    await caller
    assert ran == ["inner"]


async def test_quiesce_lets_work_nested_at_once_through() -> None:
    go = asyncio.Event()
    ran: list[str] = []

    async def innermost() -> None:
        ran.append("innermost")

    async def middle() -> None:
        await run_to_completion(innermost(), name="innermost")

    async def outer() -> None:
        await go.wait()
        await run_to_completion(middle(), name="middle")

    caller = asyncio.create_task(run_to_completion(outer(), name="outer"))
    await asyncio.sleep(0)
    quiesce = asyncio.create_task(quiesce_uncancellable(timeout=5))
    await asyncio.sleep(0)
    go.set()

    await asyncio.wait_for(quiesce, timeout=5)
    await caller
    assert ran == ["innermost"]


async def test_resume_reopens_a_quiesced_loop() -> None:
    async def work() -> str:
        return "ran"

    await quiesce_uncancellable(timeout=1)
    resume_uncancellable()

    assert await run_to_completion(work(), name="work") == "ran"


async def test_quiesce_calls_share_the_first_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()
    timeouts: list[float | None] = []
    real_wait = uncancellable.wait

    async def _recording_wait(fs: Any, *, timeout: float | None = None) -> Any:
        timeouts.append(timeout)
        return await real_wait(fs, timeout=timeout)

    monkeypatch.setattr(uncancellable, "wait", _recording_wait)
    caller = asyncio.create_task(run_to_completion(release.wait(), name="stuck"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    await quiesce_uncancellable(timeout=0.05)
    await quiesce_uncancellable(timeout=5)

    assert timeouts[-2] == pytest.approx(0.05, abs=0.01)
    assert timeouts[-1] == 0.0
    release.set()
    await asyncio.sleep(0)
