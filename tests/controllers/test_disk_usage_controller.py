"""Tests for the disk usage controller's refresh plumbing."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers import disk_usage as module
from esphome_device_builder.controllers.disk_usage import DiskUsageController
from esphome_device_builder.helpers.event_bus import EventBus
from esphome_device_builder.models import DiskUsage, EventType

_TIMEOUT = 5.0


def _usage(**overrides: Any) -> DiskUsage:
    base = DiskUsage(
        total_bytes=100,
        free_bytes=40,
        builds_bytes=10,
        remote_builds_bytes=0,
        toolchains_bytes=30,
        cache_bytes=5,
        config_bytes=1,
        measured_at=1.0,
    )
    return replace(base, **overrides)


@pytest.fixture
def harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[DiskUsageController, EventBus, list[str], asyncio.Queue[DiskUsage]]:
    """Build a controller on a real bus with the walkers stubbed and the delays removed."""
    monkeypatch.setattr(module, "_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(module, "_INITIAL_DELAY_SECONDS", 3600.0)
    calls: list[str] = []
    walks = iter(_usage(measured_at=float(n)) for n in range(1, 100))

    def _measure(*_args: Any) -> DiskUsage:
        calls.append("full")
        return next(walks)

    def _refresh(usage: DiskUsage, *_args: Any) -> DiskUsage:
        calls.append("free_space")
        return replace(usage, free_bytes=usage.free_bytes - 1)

    monkeypatch.setattr(module, "measure_disk_usage", _measure)
    monkeypatch.setattr(module, "refresh_volume_space", _refresh)
    bus = EventBus()
    db = MagicMock()
    db.bus = bus
    db.settings.config_dir = tmp_path
    fired: asyncio.Queue[DiskUsage] = asyncio.Queue()
    bus.add_listener(
        EventType.DISK_USAGE_UPDATED, lambda event: fired.put_nowait(event.data["disk_usage"])
    )
    return DiskUsageController(db), bus, calls, fired


async def test_finished_job_triggers_a_walk_and_event(harness: Any) -> None:
    controller, bus, calls, fired = harness
    await controller.start()
    try:
        assert controller.snapshot() is None
        bus.fire(EventType.JOB_COMPLETED, {})
        usage = await asyncio.wait_for(fired.get(), _TIMEOUT)
        assert calls == ["full"]
        assert controller.snapshot() == usage
    finally:
        await controller.stop()


async def test_job_burst_coalesces_into_one_walk(harness: Any) -> None:
    controller, bus, calls, fired = harness
    await controller.start()
    try:
        for event_type in (EventType.JOB_COMPLETED, EventType.JOB_FAILED, EventType.JOB_CANCELLED):
            bus.fire(event_type, {})
        await asyncio.wait_for(fired.get(), _TIMEOUT)
        await controller._worker.wait_idle()
        assert calls == ["full"]
    finally:
        await controller.stop()


async def test_free_space_pass_skips_the_walk(harness: Any) -> None:
    controller, bus, calls, fired = harness
    await controller.start()
    try:
        bus.fire(EventType.JOB_COMPLETED, {})
        first = await asyncio.wait_for(fired.get(), _TIMEOUT)
        controller._worker.request(module._Pass.FREE_SPACE)
        second = await asyncio.wait_for(fired.get(), _TIMEOUT)
        assert calls == ["full", "free_space"]
        assert second.free_bytes == first.free_bytes - 1
        assert second.measured_at == first.measured_at
    finally:
        await controller.stop()


async def test_free_space_pass_before_any_walk_does_a_full_walk(harness: Any) -> None:
    controller, _bus, calls, fired = harness
    await controller.start()
    try:
        controller._worker.request(module._Pass.FREE_SPACE)
        await asyncio.wait_for(fired.get(), _TIMEOUT)
        assert calls == ["full"]
    finally:
        await controller.stop()


async def test_unchanged_measurement_fires_nothing(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, bus, _calls, fired = harness
    monkeypatch.setattr(module, "measure_disk_usage", lambda *_a: _usage())
    await controller.start()
    try:
        bus.fire(EventType.JOB_COMPLETED, {})
        await asyncio.wait_for(fired.get(), _TIMEOUT)
        bus.fire(EventType.JOB_COMPLETED, {})
        await asyncio.sleep(0)
        await controller._worker.wait_idle()
        assert fired.empty()
    finally:
        await controller.stop()


async def test_failed_walk_is_logged_and_the_worker_survives(
    harness: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    controller, bus, _calls, fired = harness

    def _boom(*_args: Any) -> DiskUsage:
        raise OSError("disk went away")

    monkeypatch.setattr(module, "measure_disk_usage", _boom)
    await controller.start()
    try:
        bus.fire(EventType.JOB_COMPLETED, {})
        await asyncio.sleep(0)
        await controller._worker.wait_idle()
        assert "Disk usage pass" in caplog.text
        monkeypatch.setattr(module, "measure_disk_usage", lambda *_a: _usage())
        bus.fire(EventType.JOB_COMPLETED, {})
        assert await asyncio.wait_for(fired.get(), _TIMEOUT) == _usage()
    finally:
        await controller.stop()


async def test_device_removed_triggers_a_walk(harness: Any) -> None:
    controller, bus, calls, fired = harness
    await controller.start()
    try:
        bus.fire(EventType.DEVICE_REMOVED, {})
        await asyncio.wait_for(fired.get(), _TIMEOUT)
        assert calls == ["full"]
    finally:
        await controller.stop()


async def test_request_measure_queues_a_full_walk(harness: Any) -> None:
    controller, bus, calls, fired = harness
    await controller.start()
    try:
        bus.fire(EventType.JOB_COMPLETED, {})
        await asyncio.wait_for(fired.get(), _TIMEOUT)
        controller.request_measure()
        await asyncio.wait_for(fired.get(), _TIMEOUT)
        assert calls == ["full", "full"]
    finally:
        await controller.stop()


async def test_free_space_pass_reads_the_build_volumes(
    harness: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, bus, _calls, fired = harness
    build_volume = tmp_path / "build-volume"
    monkeypatch.setattr(module, "build_roots", lambda *_a: [build_volume])
    read: list[tuple[Path, ...]] = []

    def _refresh(usage: DiskUsage, paths: tuple[Path, ...]) -> DiskUsage:
        read.append(paths)
        return replace(usage, free_bytes=0)

    monkeypatch.setattr(module, "refresh_volume_space", _refresh)
    await controller.start()
    try:
        bus.fire(EventType.JOB_COMPLETED, {})
        await asyncio.wait_for(fired.get(), _TIMEOUT)
        controller._worker.request(module._Pass.FREE_SPACE)
        await asyncio.wait_for(fired.get(), _TIMEOUT)
        assert read[0][1:] == (build_volume,)
    finally:
        await controller.stop()


async def test_stop_unsubscribes(harness: Any) -> None:
    controller, bus, calls, _fired = harness
    await controller.start()
    await controller.stop()
    bus.fire(EventType.JOB_COMPLETED, {})
    await asyncio.sleep(0)
    assert calls == []


async def test_timer_walks_once_then_refreshes_free_space(
    harness: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _bus, calls, fired = harness
    monkeypatch.setattr(module, "_INITIAL_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(module, "_FREE_SPACE_INTERVAL_SECONDS", 0.0)
    await controller.start()
    try:
        await asyncio.wait_for(fired.get(), _TIMEOUT)
        await asyncio.wait_for(fired.get(), _TIMEOUT)
        assert calls[0] == "full"
        assert "free_space" in calls[1:]
    finally:
        await controller.stop()
