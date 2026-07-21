"""Contract tests for the shared presence-gated loop primitive."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from esphome_device_builder.helpers.presence_gated_loop import PresenceGatedLoop
from esphome_device_builder.helpers.subscriber_presence import SubscriberPresence


class _RecordingLoop(PresenceGatedLoop):
    _label = "test loop"
    _interval = 0.005

    def __init__(self, presence: SubscriberPresence | None) -> None:
        super().__init__(presence)
        self.ticks = 0
        self.resumes = 0
        self.work_error: BaseException | None = None
        self._ticked = asyncio.Event()

    async def wait_for_ticks(self, n: int) -> None:
        """Park until ``_work`` has run *n* times (event-driven, no polling)."""
        async with asyncio.timeout(1):
            while self.ticks < n:
                await self._ticked.wait()
                self._ticked.clear()

    def _on_resume(self) -> None:
        self.resumes += 1

    async def _work(self) -> None:
        self.ticks += 1
        self._ticked.set()
        if self.work_error is not None:
            raise self.work_error


@contextlib.asynccontextmanager
async def _running(loop: PresenceGatedLoop) -> AsyncIterator[asyncio.Task[None]]:
    task = asyncio.create_task(loop.run())
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_runs_unconditionally_without_presence() -> None:
    """presence=None means no gate: ticks flow with no subscriber anywhere."""
    loop = _RecordingLoop(None)
    async with _running(loop):
        await loop.wait_for_ticks(2)
    assert loop.resumes == 0


async def test_parks_until_first_subscriber_then_resumes() -> None:
    """The loop parks with no work done, and a real park fires ``_on_resume``."""
    presence = SubscriberPresence()
    loop = _RecordingLoop(presence)
    async with _running(loop):
        for _ in range(10):
            await asyncio.sleep(0)
        assert loop.ticks == 0
        with presence.subscriber():
            await loop.wait_for_ticks(1)
        assert loop.resumes == 1


async def test_no_resume_hook_when_gate_already_open() -> None:
    """Ticks entered with a subscriber present skip ``_on_resume``."""
    presence = SubscriberPresence()
    loop = _RecordingLoop(presence)
    with presence.subscriber():
        async with _running(loop):
            await loop.wait_for_ticks(3)
    assert loop.resumes == 0


async def test_subscriber_return_cuts_idle_short() -> None:
    """A 0→1 transition mid-idle wakes the loop without waiting out the interval."""
    presence = SubscriberPresence()
    loop = _RecordingLoop(presence)
    loop._interval = 60
    async with _running(loop):
        with presence.subscriber():
            await loop.wait_for_ticks(1)
        with presence.subscriber():
            await loop.wait_for_ticks(2)


async def test_wake_mid_work_short_circuits_following_idle() -> None:
    """A wake fired during ``_work`` survives the pre-work clear."""

    class _SelfWaking(_RecordingLoop):
        async def _work(self) -> None:
            await super()._work()
            if self.ticks == 1:
                self.wake()

    loop = _SelfWaking(None)
    loop._interval = 60
    async with _running(loop):
        await loop.wait_for_ticks(2)


async def test_continue_on_error_logs_and_keeps_looping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = _RecordingLoop(None)
    loop.work_error = RuntimeError("boom")
    async with _running(loop):
        await loop.wait_for_ticks(2)
    assert "test loop failed; continuing" in caplog.text


async def test_propagate_on_error_kills_the_loop() -> None:
    loop = _RecordingLoop(None)
    loop._continue_on_error = False
    loop.work_error = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        async with asyncio.timeout(1):
            await loop.run()
    assert loop.ticks == 1


async def test_cancellation_is_never_swallowed() -> None:
    """CancelledError raised inside ``_work`` tears the loop down."""
    started = asyncio.Event()

    class _Hanging(PresenceGatedLoop):
        _label = "hanging"

        async def _work(self) -> None:
            started.set()
            await asyncio.sleep(3600)

    loop = _Hanging(None)
    task = asyncio.create_task(loop.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_prepare_false_disables_the_loop() -> None:
    class _Disabled(_RecordingLoop):
        async def _prepare(self) -> bool:
            return False

    loop = _Disabled(None)
    async with asyncio.timeout(1):
        await loop.run()
    assert loop.ticks == 0


async def test_unsubscribe_detaches_wake_callback_and_is_idempotent() -> None:
    presence = SubscriberPresence()
    loop = _RecordingLoop(presence)
    assert len(presence._subscriber_callbacks) == 1
    loop.unsubscribe()
    assert presence._subscriber_callbacks == []
    loop.unsubscribe()
    assert presence._subscriber_callbacks == []
