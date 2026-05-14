"""
Tests for ``PingSource.probe_device`` and ``DeviceStateMonitor.probe_device_ping``.

A YAML dropped on disk for a ping-only device (no
``_esphomelib._tcp`` broadcast) would otherwise sit at UNKNOWN
until the next scheduled ICMP sweep (up to ``_PING_INTERVAL``
seconds), blocking the log-stream UI on the freshly-created
card. The eager per-device probe closes that window down to an
ICMP round-trip.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.models import Device, DeviceState

from .conftest import make_state_monitor_with_callbacks


def _ping_only_device() -> Device:
    """Build a no-API device (ICMP-reachable only) for the test fixtures."""
    return Device(
        name="garage",
        friendly_name="Garage",
        configuration="garage.yaml",
        address="192.168.1.42",
        state=DeviceState.UNKNOWN,
        loaded_integrations=["wifi"],
    )


@pytest.mark.asyncio
async def test_probe_device_noop_during_bootstrap() -> None:
    """During the bootstrap window the probe is a no-op.

    Cold-start ``ScanChange.ADDED`` fires once per cached YAML;
    if every one of those triggered an immediate ICMP, a 100-
    device fleet would emit 100 concurrent pings before mDNS even
    had its grace period to claim the API devices for free. The
    flag-gate makes the cold-start case fall back to the next
    scheduled sweep.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([_ping_only_device()])
    assert monitor._ping._bootstrap_complete is False

    await monitor._ping.probe_device("garage")

    # No state change, no DNS lookup, no ICMP: completely silent.
    assert callbacks.calls == []


@pytest.mark.asyncio
async def test_probe_device_pings_after_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """After bootstrap, an alive ICMP target flips the device ONLINE.

    The end-to-end contract: an immediate probe on a ping-only
    device's newly-dropped YAML lands the card at ONLINE within
    one ICMP round-trip instead of waiting on ``_PING_INTERVAL``.
    """
    monitor, callbacks = make_state_monitor_with_callbacks([_ping_only_device()])
    monitor._ping._bootstrap_complete = True

    ping_targets: list[str] = []

    async def _fake_ping(target: str, **_kwargs: object) -> MagicMock:
        ping_targets.append(target)
        result = MagicMock()
        result.is_alive = True
        result.min_rtt = 4.2
        return result

    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        _fake_ping,
    )

    await monitor._ping.probe_device("garage")

    assert ping_targets == ["192.168.1.42"]
    assert ("on_state_change", "garage", DeviceState.ONLINE, "ping") in callbacks.calls


@pytest.mark.asyncio
async def test_probe_device_unknown_name_is_noop() -> None:
    """A probe for a name not in the catalog short-circuits silently."""
    monitor, callbacks = make_state_monitor_with_callbacks([_ping_only_device()])
    monitor._ping._bootstrap_complete = True

    await monitor._ping.probe_device("not-a-device")

    assert callbacks.calls == []


@pytest.mark.asyncio
async def test_probe_device_skipped_when_higher_priority_source_owns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An mDNS-claimed device doesn't get a redundant ping.

    Mirrors :func:`shared.should_ping`: once mDNS owns the
    device at ONLINE the ping source has nothing to add. Without
    this guard, dropping a new YAML for a device that just
    announced via mDNS would still fire an unnecessary ICMP.
    """
    device = _ping_only_device()
    device.state = DeviceState.ONLINE
    monitor, _callbacks = make_state_monitor_with_callbacks([device])
    monitor.state.state_source["garage"] = "mdns"
    monitor._ping._bootstrap_complete = True

    ping_calls: list[str] = []

    async def _fake_ping(target: str, **_kwargs: object) -> MagicMock:
        ping_calls.append(target)
        return MagicMock(is_alive=True, min_rtt=1.0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        _fake_ping,
    )

    await monitor._ping.probe_device("garage")

    assert ping_calls == []


@pytest.mark.asyncio
async def test_probe_device_ping_skips_scheduling_during_bootstrap() -> None:
    """No task is allocated until bootstrap completes.

    Critical for cold-start: the scanner fires ``ScanChange.ADDED``
    once per cached YAML, and without this guard a 1000-device
    fleet would allocate 1000 coroutines and 1000 tasks just to
    have each one short-circuit internally. The hoisted guard
    makes the wrapper a no-op so the storm never reaches the
    scheduler.
    """
    monitor, _ = make_state_monitor_with_callbacks([_ping_only_device()])
    assert monitor._ping._bootstrap_complete is False

    monitor.probe_device_ping("garage")

    assert monitor._tasks == set()


@pytest.mark.asyncio
async def test_probe_device_ping_schedules_task_after_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-bootstrap the wrapper hands a coroutine to ``_track_task``."""

    # Stub ``icmp_ping`` so the scheduled task can't fire a real
    # ICMP probe at ``192.168.1.42`` (up to 3s flake on machines
    # with icmplib installed); this test only verifies scheduling.
    async def _noop_ping(_target: str, **_kwargs: object) -> MagicMock:
        return MagicMock(is_alive=False, min_rtt=0.0)

    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_state_monitor.ping.icmp_ping",
        _noop_ping,
    )

    monitor, _ = make_state_monitor_with_callbacks([_ping_only_device()])
    monitor._ping._bootstrap_complete = True

    monitor.probe_device_ping("garage")

    assert len(monitor._tasks) == 1
    await asyncio.gather(*monitor._tasks, return_exceptions=True)
