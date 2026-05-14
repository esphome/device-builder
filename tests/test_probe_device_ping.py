"""
Tests for ``DeviceStateMonitor.probe_device_ping`` waking the ICMP sweep loop.

A YAML dropped on disk for a ping-only device (no
``_esphomelib._tcp`` broadcast) would otherwise sit at UNKNOWN
until the next scheduled ICMP sweep (up to ``_PING_INTERVAL``
seconds), blocking the log-stream UI on the freshly-created
card. Waking the loop closes that window down to one sweep
round, with N concurrent adds collapsing into a single set.
"""

from __future__ import annotations

import asyncio

import pytest

from esphome_device_builder.controllers._device_state_monitor import ping as ping_module
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


@pytest.mark.asyncio
async def test_probe_device_ping_sets_wake_event() -> None:
    """One probe call flips the loop's wake event without scheduling a task."""
    monitor, _ = make_state_monitor_with_callbacks([_ping_only_device()])
    assert monitor._ping._wake.is_set() is False

    monitor.probe_device_ping("garage")

    assert monitor._ping._wake.is_set() is True
    assert monitor._tasks == set()


@pytest.mark.asyncio
async def test_probe_device_ping_herd_collapses_to_single_set() -> None:
    """N concurrent scanner-ADDEDs collapse into one wake — no per-device task explosion.

    The thundering-herd guard: a cold-start fleet of 100 cached
    YAMLs fires 100 ``ScanChange.ADDED`` events, but the loop's
    next sweep covers them all in one pass instead of
    spawning 100 redundant probe tasks competing for the
    ``_PING_BATCH_SIZE`` semaphore.
    """
    devices = [_ping_only_device(f"dev-{i}") for i in range(100)]
    monitor, _ = make_state_monitor_with_callbacks(devices)

    for device in devices:
        monitor.probe_device_ping(device.name)

    assert monitor._ping._wake.is_set() is True
    assert monitor._tasks == set()


@pytest.mark.asyncio
async def test_wake_bails_idle_wait_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wake fired during the idle wait re-runs the sweep without paying ``_PING_INTERVAL``."""
    monitor, _ = make_state_monitor_with_callbacks([_ping_only_device()])

    sweep_count = 0

    async def _fake_sweep(self: ping_module.PingSource) -> None:
        nonlocal sweep_count
        sweep_count += 1

    async def _fake_resolve_non_api(_monitor: object) -> None:
        return None

    monkeypatch.setattr(ping_module.PingSource, "_ping_sweep", _fake_sweep)
    monkeypatch.setattr(ping_module.shared, "resolve_non_api_mdns_targets", _fake_resolve_non_api)
    monkeypatch.setattr(ping_module, "_PING_BOOTSTRAP_DELAY", 0)
    # Long enough that a real sleep would never fire inside the
    # test window — the only path to a second sweep is the wake.
    monkeypatch.setattr(ping_module, "_PING_INTERVAL", 3600)

    task = asyncio.create_task(monitor._ping.run())
    try:
        # Yield until the first sweep lands.
        for _ in range(50):
            await asyncio.sleep(0)
            if sweep_count >= 1:
                break
        assert sweep_count == 1

        monitor.probe_device_ping("garage")

        for _ in range(50):
            await asyncio.sleep(0)
            if sweep_count >= 2:
                break
        assert sweep_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_wake_during_sweep_triggers_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wake set while a sweep is in flight survives to fire one more sweep.

    Pins the ``_wake.clear()``-before-sweep ordering: a device
    added after the sweep snapshotted ``_get_devices()`` would
    otherwise be missed entirely until the next scheduled
    interval. Clearing before the sweep means a wake fired
    mid-sweep stays set into the idle wait, which bails
    immediately and triggers a follow-up.
    """
    monitor, _ = make_state_monitor_with_callbacks([_ping_only_device()])

    sweep_count = 0
    sweep_started = asyncio.Event()
    sweep_release = asyncio.Event()

    async def _fake_sweep(self: ping_module.PingSource) -> None:
        nonlocal sweep_count
        sweep_count += 1
        if sweep_count == 1:
            sweep_started.set()
            await sweep_release.wait()

    async def _fake_resolve_non_api(_monitor: object) -> None:
        return None

    monkeypatch.setattr(ping_module.PingSource, "_ping_sweep", _fake_sweep)
    monkeypatch.setattr(ping_module.shared, "resolve_non_api_mdns_targets", _fake_resolve_non_api)
    monkeypatch.setattr(ping_module, "_PING_BOOTSTRAP_DELAY", 0)
    monkeypatch.setattr(ping_module, "_PING_INTERVAL", 3600)

    task = asyncio.create_task(monitor._ping.run())
    try:
        await asyncio.wait_for(sweep_started.wait(), timeout=1)
        # Fire the wake while sweep #1 is held; sweep #2 must run.
        monitor.probe_device_ping("garage")
        sweep_release.set()

        for _ in range(50):
            await asyncio.sleep(0)
            if sweep_count >= 2:
                break
        assert sweep_count == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
