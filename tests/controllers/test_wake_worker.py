"""Tests for the shared :class:`WakeWorker` base."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from esphome_device_builder.controllers._wake_worker import WakeWorker


class _RecordingWorker(WakeWorker[str]):
    """Concrete worker that captures every drain into a list."""

    def __init__(self) -> None:
        super().__init__()
        self.drained: list[list[str]] = []
        self.started = False

    async def _on_start(self) -> None:
        self.started = True

    async def _drain(self) -> None:
        items = sorted(self.pending)
        self.pending.clear()
        self.drained.append(items)


class _WedgedWorker(WakeWorker[str]):
    """Worker whose run loop never completes a drain."""

    async def _drain(self) -> None:
        # Hijack the entire loop so wait_idle would otherwise hang.
        await asyncio.Event().wait()


async def test_request_populates_pending_and_clears_idle() -> None:
    """``request`` is sync, deduplicates, clears idle, sets wake."""
    worker = _RecordingWorker()
    worker.request("a")
    worker.request("a")
    worker.request("b")
    assert worker.pending == {"a", "b"}
    assert worker._wake.is_set()
    assert not worker._idle.is_set()


async def test_drain_clears_wake_and_sets_idle_on_exit() -> None:
    """Drain context manager clears wake on entry, sets idle on empty-pending exit."""
    worker = _RecordingWorker()
    worker.request("a")
    async with worker._drain_cycle():
        assert not worker._wake.is_set()
        worker.pending.clear()
    assert worker._idle.is_set()


async def test_wait_idle_blocks_until_drain_completes() -> None:
    """``wait_idle`` returns only after the drain processes every request."""
    worker = _RecordingWorker()
    worker.start()
    try:
        worker.request("a")
        worker.request("b")
        await worker.wait_idle()
    finally:
        await worker.stop()
    assert worker.drained == [["a", "b"]]
    assert worker.started


async def test_wait_idle_stays_clear_if_request_lands_mid_drain() -> None:
    """Mid-drain request keeps idle clear; both items end up drained."""

    class _ChainingWorker(WakeWorker[str]):
        def __init__(self) -> None:
            super().__init__()
            self.drained: list[list[str]] = []

        async def _drain(self) -> None:
            items = sorted(self.pending)
            self.pending.clear()
            self.drained.append(items)
            if items == ["a"]:
                self.request("b")

    worker = _ChainingWorker()
    worker.start()
    try:
        worker.request("a")
        await worker.wait_idle()
    finally:
        await worker.stop()
    assert worker.drained == [["a"], ["b"]]


async def test_wait_idle_returns_after_stop() -> None:
    """``stop`` unblocks any ``wait_idle`` parked through shutdown."""
    worker = _WedgedWorker()
    worker.start()
    worker.request("never-drained")
    waiter = asyncio.create_task(worker.wait_idle())
    await asyncio.sleep(0.01)
    assert not waiter.done()
    await worker.stop()
    await asyncio.wait_for(waiter, timeout=1.0)


async def test_start_is_idempotent() -> None:
    """A second ``start`` while the worker is alive is a no-op."""
    worker = _RecordingWorker()
    worker.start()
    first = worker._task
    try:
        worker.start()
        assert worker._task is first
    finally:
        await worker.stop()


async def test_stop_cancels_and_clears_task() -> None:
    """``stop`` cancels, awaits, clears the task; idempotent."""
    worker = _RecordingWorker()
    worker.start()
    assert worker._task is not None
    await worker.stop()
    assert worker._task is None
    await worker.stop()  # idempotent


async def test_stop_with_no_running_worker_is_noop() -> None:
    """``stop`` on a never-started worker returns cleanly."""
    worker = _RecordingWorker()
    await worker.stop()
    assert worker._task is None


@pytest.mark.asyncio
async def test_stop_logs_unexpected_exception(caplog: Any) -> None:
    """A non-cancel exception from the worker is logged during ``stop``."""
    entered_drain = asyncio.Event()

    class _Exploding(WakeWorker[str]):
        async def _drain(self) -> None:
            entered_drain.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("boom") from None

    caplog.set_level(logging.ERROR)
    worker = _Exploding()
    worker.start()
    # Request kicks the loop into _drain; without this the worker
    # would be parked on _wake.wait and the cancellation wouldn't
    # reach the RuntimeError branch inside _drain.
    worker.request("x")
    await entered_drain.wait()
    await worker.stop()

    expected = f"Worker {_Exploding.__name__} failed during shutdown"
    assert any(expected in r.message for r in caplog.records)
