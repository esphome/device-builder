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


async def test_wait_clears_event() -> None:
    """``wait`` returns when the wake fires and leaves the event cleared."""
    worker: WakeWorker[str] = WakeWorker()
    worker.request("a")
    await worker.wait()
    assert not worker._wake.is_set()


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
