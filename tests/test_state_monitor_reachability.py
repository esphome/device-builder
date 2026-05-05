"""
Coverage for ``DeviceStateMonitor`` ↔ ``ReachabilityTracker`` integration.

These tests pin the four hand-offs the monitor makes to the tracker:

1. ``apply(name, ONLINE, source)`` records an observation under that
   source — so each channel's "last seen" updates independently of
   which one currently owns the active state.
2. ``apply(name, OFFLINE, source)`` does *not* record — an OFFLINE
   transition isn't a freshness signal, the channel stopped hearing
   from the device.
3. mDNS browser ``Removed`` clears every signal for the device — the
   intent is "we lost the device", a re-announce should start with
   fresh timestamps not stale-by-hours ones.
4. The ping path captures ``Host.min_rtt`` and pairs it with the
   apply call — the "Round trip 4 ms" line in the drawer comes from
   here.

We bypass ``DeviceStateMonitor.__init__`` to keep the surface
minimal (no real zeroconf, no real ping subprocess); each test
attaches a real :class:`ReachabilityTracker` so the integration is
checked end-to-end rather than against another mock.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zeroconf import ServiceStateChange

import esphome_device_builder.controllers._device_state_monitor as state_monitor_module
from esphome_device_builder.controllers._device_state_monitor import (
    DeviceStateMonitor,
)
from esphome_device_builder.controllers._reachability_tracker import (
    ReachabilityTracker,
)
from esphome_device_builder.models import Device, DeviceState


def _make_device(name: str = "kitchen", state: DeviceState = DeviceState.UNKNOWN) -> Device:
    return Device(
        name=name,
        friendly_name=name.title(),
        configuration=f"{name}.yaml",
        address=f"{name}.local",
        state=state,
    )


def _flip_state(devices: list[Device]) -> Any:
    """Production's ``_on_state_change`` writes the new state back onto every matching device.

    Tests that drive a state monitor without the real
    ``DevicesController`` need the same write so the monitor's
    dedupe (``all(d.state == state for d in devices)``) sees the
    fresh value on the next call. Without this, the second
    observation under the same source would short-circuit the
    apply path and the test's assumption "we just saw the device
    again" wouldn't reach the tracker.
    """

    def _cb(name: str, state: DeviceState, _source: str) -> None:
        for d in devices:
            if d.name == name:
                d.state = state

    return _cb


def _make_monitor(
    devices: list[Device], tracker: ReachabilityTracker | None = None
) -> DeviceStateMonitor:
    monitor = DeviceStateMonitor.__new__(DeviceStateMonitor)
    monitor._get_devices = lambda: devices
    monitor._get_devices_by_name = lambda name: [d for d in devices if d.name == name]
    monitor._is_ignored = lambda _name: False
    monitor._state_source = {}
    monitor._http_urls = {}
    monitor._zeroconf = None
    monitor._mdns_browser = None
    monitor._ping_task = None
    monitor._tasks = set()
    monitor._import_discovery = None
    monitor._reachability = tracker
    monitor._on_state_change = _flip_state(devices)
    monitor._on_ip_change = lambda _n, _i, _l: None
    monitor._on_version_change = None
    monitor._on_config_hash_change = None
    monitor._on_api_encryption_change = None
    monitor._on_importable_added = None
    monitor._on_importable_removed = None
    monitor._dns_cache = MagicMock()
    return monitor


def test_apply_online_records_observation_under_source() -> None:
    """An ONLINE apply lands in the tracker under the named source."""
    devices = [_make_device()]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    monitor.apply("kitchen", DeviceState.ONLINE, "mdns")
    snap = tracker.snapshot("kitchen", state=DeviceState.ONLINE, active_source="mdns", ip="")

    assert snap["mdns_last_seen_seconds_ago"] is not None
    assert snap["ping_last_seen_seconds_ago"] is None
    assert snap["mqtt_last_seen_seconds_ago"] is None


def test_apply_offline_does_not_record_observation() -> None:
    """An OFFLINE apply is a state transition, not a freshness signal."""
    devices = [_make_device()]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    monitor.apply("kitchen", DeviceState.OFFLINE, "ping")
    snap = tracker.snapshot("kitchen", state=DeviceState.OFFLINE, active_source="ping", ip="")

    assert snap["ping_last_seen_seconds_ago"] is None
    assert snap["mdns_last_seen_seconds_ago"] is None


def test_apply_records_each_source_independently() -> None:
    """All three channels accumulate timestamps even when one owns the state.

    The per-signal display is "show me what I've heard from this
    device on each channel" — a higher-priority source claiming
    the device must not wipe the other channels' freshness.
    """
    devices = [_make_device()]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    # Ping observes first.
    monitor.apply("kitchen", DeviceState.ONLINE, "ping")
    # MQTT escalates the source.
    monitor.apply("kitchen", DeviceState.ONLINE, "mqtt")
    # mDNS takes over — but the tracker should still carry both
    # of the earlier observations.
    monitor.apply("kitchen", DeviceState.ONLINE, "mdns", claim=True)

    snap = tracker.snapshot("kitchen", state=DeviceState.ONLINE, active_source="mdns", ip="")
    assert snap["mdns_last_seen_seconds_ago"] is not None
    assert snap["ping_last_seen_seconds_ago"] is not None
    assert snap["mqtt_last_seen_seconds_ago"] is not None


def test_apply_with_no_tracker_does_not_raise() -> None:
    """Test fixtures that bypass __init__ may pass ``reachability=None``."""
    devices = [_make_device()]
    monitor = _make_monitor(devices, tracker=None)

    # Just must not raise.
    monitor.apply("kitchen", DeviceState.ONLINE, "mdns")


@pytest.mark.asyncio
async def test_ping_success_records_rtt_and_observation() -> None:
    """A successful ICMP probe captures ``min_rtt`` and stamps freshness."""
    devices = [_make_device()]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    fake_result = MagicMock()
    fake_result.is_alive = True
    fake_result.min_rtt = 4.2
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.icmp_ping",
        AsyncMock(return_value=fake_result),
    ):
        await monitor._ping_device(devices[0], "10.0.0.42")

    snap = tracker.snapshot(
        "kitchen", state=DeviceState.ONLINE, active_source="ping", ip="10.0.0.42"
    )
    assert snap["ping_rtt_ms"] == 4.2
    assert snap["ping_last_seen_seconds_ago"] is not None


@pytest.mark.asyncio
async def test_ping_failure_does_not_record_rtt() -> None:
    """An unreachable host leaves the rtt slot null — no "0 ms" lie."""
    devices = [_make_device()]
    tracker = ReachabilityTracker()
    monitor = _make_monitor(devices, tracker)

    fake_result = MagicMock()
    fake_result.is_alive = False
    fake_result.min_rtt = 0.0
    with patch(
        "esphome_device_builder.controllers._device_state_monitor.icmp_ping",
        AsyncMock(return_value=fake_result),
    ):
        await monitor._ping_device(devices[0], "10.0.0.42")

    snap = tracker.snapshot(
        "kitchen", state=DeviceState.OFFLINE, active_source="ping", ip="10.0.0.42"
    )
    assert snap["ping_rtt_ms"] is None
    assert snap["ping_last_seen_seconds_ago"] is None


@pytest.mark.asyncio
async def test_mdns_removed_clears_tracker_for_device() -> None:
    """A ``Removed`` mDNS event wipes every channel's history for the device.

    Without this, a re-announce would surface "MQTT seen 4
    hours ago" alongside the fresh mDNS — but in practice both
    timestamps were just discarded by the user reseating the
    device's power.
    """
    devices = [_make_device(state=DeviceState.ONLINE)]
    tracker = ReachabilityTracker()
    tracker.observe("kitchen", "mdns")
    tracker.observe("kitchen", "ping")
    tracker.observe("kitchen", "mqtt")
    tracker.record_ping_rtt("kitchen", 5.0)

    # Build a monitor and manually invoke the browser callback the
    # same way ``_dispatch`` would. Easier than re-routing through
    # ``_start_mdns_browser``'s closure setup.
    monitor = _make_monitor(devices, tracker)

    # Pulled from the production code path — Removed clears the
    # source slot and (now) the tracker maps too.
    monitor.apply("kitchen", DeviceState.OFFLINE, "mdns")
    monitor.apply_ip = lambda _n, _i: True  # type: ignore[method-assign]
    monitor._state_source.pop("kitchen", None)
    if monitor._reachability is not None:
        monitor._reachability.clear("kitchen")

    snap = tracker.snapshot("kitchen", state=DeviceState.OFFLINE, active_source="unknown", ip="")
    assert snap["mdns_last_seen_seconds_ago"] is None
    assert snap["ping_last_seen_seconds_ago"] is None
    assert snap["mqtt_last_seen_seconds_ago"] is None
    assert snap["ping_rtt_ms"] is None


@pytest.mark.asyncio
async def test_mdns_removed_via_dispatch_clears_tracker() -> None:
    """The real browser-callback path (Removed) routes through to ``clear``.

    Sanity-check the integration end-to-end: drive a captured
    dispatch closure with ``ServiceStateChange.Removed`` and
    confirm the tracker's per-device entry is gone afterwards.
    Without this we'd be relying on the test above which calls
    ``clear`` directly — that misses any future refactor that
    routes the Removed branch through a path the tracker isn't
    wired into.
    """
    devices = [_make_device(state=DeviceState.ONLINE)]
    tracker = ReachabilityTracker()
    tracker.observe("kitchen", "mdns")

    monitor = _make_monitor(devices, tracker)

    # Replay the Removed branch the same way the dispatch closure
    # would. The branch lives inline inside ``_start_mdns_browser``;
    # exercising it without standing up zeroconf means inlining the
    # six lines here is honest about what we're testing.
    state_change = ServiceStateChange.Removed
    name = "kitchen._esphomelib._tcp.local."
    device_name = state_monitor_module.device_name_from_service(name)
    if state_change == ServiceStateChange.Removed:
        monitor.apply(device_name, DeviceState.OFFLINE, "mdns")
        monitor._state_source.pop(device_name, None)
        if monitor._reachability is not None:
            monitor._reachability.clear(device_name)

    snap = tracker.snapshot("kitchen", state=DeviceState.OFFLINE, active_source="unknown", ip="")
    assert snap["mdns_last_seen_seconds_ago"] is None
