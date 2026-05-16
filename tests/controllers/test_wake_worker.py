"""Tests for the shared :class:`WakeWorker` primitive."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from esphome_device_builder.controllers._wake_worker import WakeWorker


async def test_request_populates_pending_and_sets_wake() -> None:
    """``request`` is sync, idempotent, signals the wake."""
    worker: WakeWorker[str] = WakeWorker()
    worker.request("a")
    worker.request("a")
    worker.request("b")
    assert worker.pending == {"a", "b"}
    assert worker._wake.is_set()


async def test_drain_clears_wake_and_sets_idle_on_exit() -> None:
    """``drain`` clears the wake on entry and idle-sets on exit when pending is empty."""
    worker: WakeWorker[str] = WakeWorker()
    worker.request("a")
    async with worker.drain():
        # Wake is cleared on entry.
        assert not worker._wake.is_set()
        # Drain body consumes pending (mirrors the owner's swap).
        worker.pending.clear()
    # Pending empty at exit → idle set.
    assert worker._idle.is_set()


async def test_wait_idle_blocks_until_drain_completes() -> None:
    """``wait_idle`` parks until the run loop finishes draining the pending set."""
    worker: WakeWorker[str] = WakeWorker()
    drained: list[str] = []

    async def _loop() -> None:
        while True:
            async with worker.drain():
                drained.extend(worker.pending)
                worker.pending.clear()

    worker.start(_loop, name="t")
    try:
        worker.request("a")
        worker.request("b")
        await worker.wait_idle()
    finally:
        await worker.stop()
    assert set(drained) == {"a", "b"}


async def test_wait_idle_returns_after_stop() -> None:
    """``stop`` unblocks any ``wait_idle`` parked through shutdown."""
    worker: WakeWorker[str] = WakeWorker()

    async def _wedged_loop() -> None:
        # Park forever — wait_idle would otherwise hang because no
        # drain ever fires. ``stop`` must still unblock it.
        await asyncio.Event().wait()

    worker.start(_wedged_loop, name="t")
    worker.request("never-drained")
    waiter = asyncio.create_task(worker.wait_idle())
    await asyncio.sleep(0.01)
    assert not waiter.done()
    await worker.stop()
    await asyncio.wait_for(waiter, timeout=1.0)


async def test_wait_idle_stays_clear_if_request_lands_mid_drain() -> None:
    """A request fired during drain keeps idle clear until that request is drained."""
    worker: WakeWorker[str] = WakeWorker()
    drained: list[str] = []
    second_request_done = asyncio.Event()

    async def _loop() -> None:
        while True:
            async with worker.drain():
                items = list(worker.pending)
                worker.pending.clear()
                drained.extend(items)
                # On the first cycle, simulate a concurrent request
                # landing mid-drain by firing it before the context
                # manager exits. ``wait_idle`` must not return until
                # the second request is also drained.
                if items == ["a"] and not second_request_done.is_set():
                    worker.request("b")
                    second_request_done.set()

    worker.start(_loop, name="t")
    try:
        worker.request("a")
        await worker.wait_idle()
    finally:
        await worker.stop()
    assert drained == ["a", "b"]


async def test_start_spawns_and_is_idempotent() -> None:
    """``start`` is idempotent — second call returns the existing task."""
    worker: WakeWorker[str] = WakeWorker()
    parked = asyncio.Event()

    async def _loop() -> None:
        parked.set()
        await asyncio.Event().wait()

    worker.start(_loop, name="t")
    first = worker._task
    await parked.wait()
    worker.start(_loop)
    assert worker._task is first
    await worker.stop()


async def test_stop_cancels_and_clears_task() -> None:
    """``stop`` cancels, awaits, and clears the task reference."""
    worker: WakeWorker[str] = WakeWorker()

    async def _loop() -> None:
        await asyncio.Event().wait()

    worker.start(_loop)
    assert worker._task is not None
    await worker.stop()
    assert worker._task is None
    await worker.stop()  # idempotent


async def test_stop_with_no_running_worker_is_noop() -> None:
    """``stop`` on a never-started worker returns cleanly."""
    worker: WakeWorker[str] = WakeWorker()
    await worker.stop()
    assert worker._task is None


@pytest.mark.asyncio
async def test_stop_logs_unexpected_exception(caplog: Any) -> None:
    """A non-cancel exception from the worker is logged during ``stop``."""
    worker: WakeWorker[str] = WakeWorker()

    async def _failing() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("boom") from None

    caplog.set_level(logging.ERROR)
    worker.start(_failing, name="boomy")
    await asyncio.sleep(0)
    await worker.stop()

    assert any("Worker boomy failed during shutdown" in r.message for r in caplog.records)
