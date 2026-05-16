"""Tests for the shared :class:`WakeWorker` base."""

from __future__ import annotations

import asyncio
import logging

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


async def test_default_drain_raises_not_implemented() -> None:
    """The base ``_drain`` is abstract — calling it raises."""
    worker: WakeWorker[str] = WakeWorker()
    with pytest.raises(NotImplementedError):
        await worker._drain()


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


async def test_drain_exception_logged_and_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unexpected raise from ``_drain`` is logged; the loop keeps draining."""

    class _FlakyDrain(WakeWorker[str]):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def _drain(self) -> None:
            self.calls += 1
            items = sorted(self.pending)
            self.pending.clear()
            if items == ["bad"]:
                raise RuntimeError("oops")

    caplog.set_level(logging.ERROR)
    worker = _FlakyDrain()
    worker.start()
    try:
        worker.request("bad")
        await worker.wait_idle()
        worker.request("good")
        await worker.wait_idle()
    finally:
        await worker.stop()

    assert worker.calls == 2
    assert any("drain raised" in r.message for r in caplog.records)


async def test_on_start_exception_logged_and_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An exception escaping ``_on_start`` is logged; the drain loop still runs."""

    class _FlakyStart(WakeWorker[str]):
        def __init__(self) -> None:
            super().__init__()
            self.processed: list[str] = []

        async def _on_start(self) -> None:
            raise RuntimeError("on_start oops")

        async def _drain(self) -> None:
            self.processed.extend(sorted(self.pending))
            self.pending.clear()

    caplog.set_level(logging.ERROR)
    worker = _FlakyStart()
    worker.start()
    try:
        worker.request("after-broken-start")
        await asyncio.wait_for(worker.wait_idle(), timeout=1.0)
    finally:
        await worker.stop()
    assert worker.processed == ["after-broken-start"]
    assert any("_on_start raised" in r.message for r in caplog.records)


async def test_drain_raising_with_items_pending_drains_remainder() -> None:
    """``_drain`` raising mid-pending must not strand the unprocessed items.

    Regression for the deadlock where ``_drain_cycle.__aexit__``
    left ``_wake`` cleared and ``_idle`` cleared, parking the
    next ``_wake.wait()`` forever and any ``wait_idle`` waiter.
    """

    class _MidPopFlaky(WakeWorker[str]):
        def __init__(self) -> None:
            super().__init__()
            self.processed: list[str] = []
            self.raised_once = False

        async def _drain(self) -> None:
            # Pop-as-you-go pattern; raise on the first item
            # while leaving the rest in pending.
            item = self.pending.pop()
            if not self.raised_once and item != "fine":
                self.raised_once = True
                raise RuntimeError("oops")
            self.processed.append(item)

    worker = _MidPopFlaky()
    worker.start()
    try:
        worker.request("bad")
        worker.request("fine")
        await asyncio.wait_for(worker.wait_idle(), timeout=1.0)
    finally:
        await worker.stop()
    # The "fine" item gets processed despite the earlier raise.
    assert "fine" in worker.processed


async def test_wait_idle_after_start_with_no_on_start_work() -> None:
    """``wait_idle`` returns after ``start`` even if ``_on_start`` queued nothing."""

    class _Quiet(WakeWorker[str]):
        async def _drain(self) -> None:
            self.pending.clear()

    worker = _Quiet()
    worker.start()
    try:
        # No request before wait_idle; _on_start did nothing.
        # wait_idle must still return (the loop re-sets idle).
        await asyncio.wait_for(worker.wait_idle(), timeout=1.0)
    finally:
        await worker.stop()


async def test_wait_idle_after_start_waits_for_on_start_work() -> None:
    """``wait_idle`` parks past ``_on_start`` queuing work via ``request``."""

    class _SeedingStart(WakeWorker[str]):
        def __init__(self) -> None:
            super().__init__()
            self.processed: list[str] = []

        async def _on_start(self) -> None:
            self.request("seeded")

        async def _drain(self) -> None:
            self.processed.extend(sorted(self.pending))
            self.pending.clear()

    worker = _SeedingStart()
    worker.start()
    try:
        await asyncio.wait_for(worker.wait_idle(), timeout=1.0)
    finally:
        await worker.stop()
    assert worker.processed == ["seeded"]


async def test_start_logs_and_replaces_crashed_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second ``start`` after a crash logs the prior exception and restarts."""

    class _Crashy(WakeWorker[str]):
        # Override _run_loop directly so the crash bypasses the
        # base's per-drain guard and the task ends with an
        # exception that ``start`` has to deal with.
        async def _run_loop(self) -> None:
            raise RuntimeError("ka-boom")

    caplog.set_level(logging.ERROR)
    worker = _Crashy()
    worker.start()
    first = worker._task
    # Let the task run and die.
    for _ in range(5):
        if first is not None and first.done():
            break
        await asyncio.sleep(0)
    assert first is not None and first.done()
    # Re-starting should retrieve the prior exception and spawn a fresh task.
    worker.start()
    assert worker._task is not first
    await worker.stop()
    assert any("crashed; restarting" in r.message for r in caplog.records)


async def test_stop_logs_unexpected_exception(caplog: pytest.LogCaptureFixture) -> None:
    """A non-cancel exception escaping ``_run_loop`` is logged during ``stop``."""

    class _Exploding(WakeWorker[str]):
        # Override _run_loop so the raise bypasses the base's per-drain
        # guard and the task ends with an exception that ``stop`` has
        # to retrieve.
        async def _run_loop(self) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("boom") from None

    caplog.set_level(logging.ERROR)
    worker = _Exploding()
    worker.start()
    await asyncio.sleep(0)
    await worker.stop()

    expected = f"Worker {_Exploding.__name__} failed during shutdown"
    assert any(expected in r.message for r in caplog.records)
