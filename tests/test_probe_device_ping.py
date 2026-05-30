"""Tests for ``DeviceStateMonitor.probe_device_ping`` waking the ICMP sweep loop."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

from esphome_device_builder.controllers._device_state_monitor import ping as ping_module
from esphome_device_builder.controllers._device_state_monitor.controller import (
    DeviceStateMonitor,
)
from esphome_device_builder.models import Device, DeviceState

from .conftest import make_state_monitor_with_callbacks


def _ping_only_device(name: str = "garage") -> Device:
    """Build a no-API device (ICMP-reachable only) for the test fixtures."""
    return Device(
        name=name,
        friendly_name=name.title(),
        configuration=f"{name}.yaml",
        address=f"{name}.local",
        state=DeviceState.UNKNOWN,
        loaded_integrations=["wifi"],
    )


@dataclass
class _SweepProbe:
    """Counts sweeps and offers a release latch for blocking the first one mid-flight."""

    count: int = 0
    first_entered: asyncio.Event = field(default_factory=asyncio.Event)
    release_first: asyncio.Event = field(default_factory=asyncio.Event)


def _install_sweep_probe(
    monkeypatch: pytest.MonkeyPatch, *, block_first: bool = False
) -> _SweepProbe:
    """Replace ``PingSource._ping_sweep`` with a stub recording into a fresh ``_SweepProbe``."""
    probe = _SweepProbe()

    async def _sweep(_self: ping_module.PingSource) -> None:
        probe.count += 1
        if probe.count == 1:
            probe.first_entered.set()
            if block_first:
                await probe.release_first.wait()

    monkeypatch.setattr(ping_module.PingSource, "_ping_sweep", _sweep)
    return probe


def _patch_loop_for_wake_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip bootstrap and stretch the interval so only wakes can drive a second sweep."""
    monkeypatch.setattr(ping_module, "_PING_BOOTSTRAP_DELAY", 0)
    monkeypatch.setattr(ping_module, "_PING_INTERVAL", 3600)

    async def _noop_resolve(_monitor: object) -> None:
        return None

    monkeypatch.setattr(ping_module.shared, "resolve_non_api_mdns_targets", _noop_resolve)


@asynccontextmanager
async def _running_loop(monitor: DeviceStateMonitor) -> AsyncIterator[None]:
    """Spawn ``monitor._ping.run()`` and cancel + drain on exit."""
    task = asyncio.create_task(monitor._ping.run())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _yield_until(predicate: Callable[[], bool], iterations: int = 50) -> None:
    """Yield to the event loop until *predicate()* is truthy or *iterations* elapse."""
    for _ in range(iterations):
        if predicate():
            return
        await asyncio.sleep(0)


def test_probe_device_ping_sets_wake_event() -> None:
    """One probe call flips the loop's wake event without scheduling a task."""
    monitor, _ = make_state_monitor_with_callbacks([_ping_only_device()])
    assert monitor._ping._wake.is_set() is False

    monitor.probe_device_ping("garage")

    assert monitor._ping._wake.is_set() is True
    assert monitor._tasks == set()


def test_probe_device_ping_herd_collapses_to_single_set() -> None:
    """N concurrent scanner-ADDEDs collapse into one wake; no per-device task explosion."""
    devices = [_ping_only_device(f"dev-{i}") for i in range(100)]
    monitor, _ = make_state_monitor_with_callbacks(devices)

    for device in devices:
        monitor.probe_device_ping(device.name)

    assert monitor._ping._wake.is_set() is True
    assert monitor._tasks == set()


async def test_wake_bails_idle_wait_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wake fired during the idle wait re-runs the sweep without paying ``_PING_INTERVAL``."""
    _patch_loop_for_wake_tests(monkeypatch)
    monitor, _ = make_state_monitor_with_callbacks([_ping_only_device()])
    sweeps = _install_sweep_probe(monkeypatch)

    async with _running_loop(monitor):
        await _yield_until(lambda: sweeps.count >= 1)
        assert sweeps.count == 1

        monitor.probe_device_ping("garage")

        await _yield_until(lambda: sweeps.count >= 2)
        assert sweeps.count == 2


async def test_wake_during_sweep_triggers_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wake fired mid-sweep survives the pre-sweep ``_wake.clear()`` to drive one follow-up."""
    _patch_loop_for_wake_tests(monkeypatch)
    monitor, _ = make_state_monitor_with_callbacks([_ping_only_device()])
    sweeps = _install_sweep_probe(monkeypatch, block_first=True)

    async with _running_loop(monitor):
        await asyncio.wait_for(sweeps.first_entered.wait(), timeout=1)
        monitor.probe_device_ping("garage")
        sweeps.release_first.set()

        await _yield_until(lambda: sweeps.count >= 2)
        assert sweeps.count == 2
