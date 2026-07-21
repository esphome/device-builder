"""Contract tests for the shared presence-gated loop primitive."""

from __future__ import annotations

import asyncio
import logging

import pytest

from esphome_device_builder.helpers.presence_gated_loop import PresenceGatedLoop
from esphome_device_builder.helpers.subscriber_presence import SubscriberPresence

from .conftest import running_task


class _RecordingLoop(PresenceGatedLoop[None]):
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


async def test_base_demands_work() -> None:
    """The bare base has no ``_work``."""
    with pytest.raises(NotImplementedError):
        await PresenceGatedLoop(None)._work()


async def test_runs_unconditionally_without_presence() -> None:
    """presence=None means no gate: ticks flow with no subscriber anywhere."""
    loop = _RecordingLoop(None)
    async with running_task(loop.run()):
        await loop.wait_for_ticks(2)
    assert loop.resumes == 0


async def test_parks_until_first_subscriber_then_resumes() -> None:
    """The loop parks with no work done, and a real park fires ``_on_resume``."""
    presence = SubscriberPresence()
    loop = _RecordingLoop(presence)
    async with running_task(loop.run()):
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
        async with running_task(loop.run()):
            await loop.wait_for_ticks(3)
    assert loop.resumes == 0


async def test_subscriber_return_cuts_idle_short() -> None:
    """A 0→1 transition mid-idle wakes the loop without waiting out the interval."""
    presence = SubscriberPresence()
    loop = _RecordingLoop(presence)
    loop._interval = 60
    async with running_task(loop.run()):
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
    async with running_task(loop.run()):
        await loop.wait_for_ticks(2)


async def test_continue_on_error_logs_and_keeps_looping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = _RecordingLoop(None)
    loop.work_error = RuntimeError("boom")
    async with running_task(loop.run()):
        await loop.wait_for_ticks(2)
    assert "test loop failed; continuing" in caplog.text


async def test_crash_continue_collapses_repeat_logs_until_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure streak logs one traceback; a successful tick re-arms the loud log."""
    loop = _RecordingLoop(None)
    loop.work_error = RuntimeError("boom")
    with caplog.at_level(logging.DEBUG, logger=PresenceGatedLoop.__module__):
        async with running_task(loop.run()):
            await loop.wait_for_ticks(2)
            loop.work_error = None
            await loop.wait_for_ticks(3)
            loop.work_error = RuntimeError("boom again")
            await loop.wait_for_ticks(4)
    messages = [(r.levelno, r.message) for r in caplog.records if "continuing" in r.message]
    errors = [m for level, m in messages if level == logging.ERROR]
    debugs = [m for level, m in messages if level == logging.DEBUG]
    assert len(errors) == 2
    assert debugs, "repeat failure in a streak must collapse to DEBUG"


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

    class _Hanging(PresenceGatedLoop[None]):
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


async def test_run_refuses_concurrent_reentry() -> None:
    """A second concurrent ``run()`` raises instead of silently double-ticking."""
    loop = _RecordingLoop(None)
    async with running_task(loop.run()):
        await loop.wait_for_ticks(1)
        with pytest.raises(RuntimeError, match="already running"):
            await loop.run()


async def test_run_is_rerunnable_after_cancellation() -> None:
    """Sequential re-runs work — the MQTT loop re-runs per broker session."""
    loop = _RecordingLoop(None)
    async with running_task(loop.run()):
        await loop.wait_for_ticks(1)
    async with running_task(loop.run()):
        await loop.wait_for_ticks(2)


async def test_after_idle_gets_the_result_and_skips_crashed_ticks() -> None:
    """``_after_idle`` sees each completed tick's ``_work`` result; crashed ticks skip it."""
    seen: list[int] = []
    done = asyncio.Event()

    class _Flaky(PresenceGatedLoop[int]):
        _label = "flaky"
        _interval = 0.001

        def __init__(self) -> None:
            super().__init__(None)
            self.ticks = 0

        async def _work(self) -> int:
            self.ticks += 1
            if self.ticks == 2:
                raise RuntimeError("boom")
            return self.ticks

        def _after_idle(self, result: int) -> None:
            seen.append(result)
            if len(seen) >= 2:
                done.set()

    loop = _Flaky()
    async with running_task(loop.run()):
        async with asyncio.timeout(1):
            await done.wait()
    assert seen[:2] == [1, 3]


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
