"""
Unit coverage for :class:`ReachabilityTracker`.

The tracker is a pure data-shape: four monotonic-time dicts plus a
snapshot serializer. These tests pin its observable contract — what
shapes ``snapshot()`` returns for empty / partial / full state, that
``clear()`` actually removes every dict's entry, that the observation
callback fires on each ``observe`` and ``record_ping_rtt`` call but
*not* on ``clear``, and that ``snapshot()`` clamps the relative-time
math to zero so a future-timed entry can't surface as ``-0.001s ago``.

We patch ``time.monotonic`` rather than relying on real wall-clock so
the relative-time assertions can be exact (no flakes from a busy CI
runner).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from esphome_device_builder.controllers._reachability_tracker import (
    ReachabilityTracker,
)
from esphome_device_builder.models import DeviceState


def _snapshot(
    tracker: ReachabilityTracker,
    name: str = "kitchen",
    *,
    state: DeviceState = DeviceState.ONLINE,
    active_source: str = "mdns",
    ip: str = "10.0.0.42",
) -> dict[str, Any]:
    """Take a snapshot with sensible defaults — keeps test bodies short."""
    return tracker.snapshot(name, state=state, active_source=active_source, ip=ip)


def test_snapshot_empty_returns_all_nulls() -> None:
    """A device with no observations gets ``None`` for every freshness field."""
    tracker = ReachabilityTracker()
    snap = _snapshot(tracker, state=DeviceState.UNKNOWN, active_source="unknown", ip="")

    assert snap == {
        "device": "kitchen",
        "state": "unknown",
        "active_source": "unknown",
        "ip": "",
        "mdns_last_seen_seconds_ago": None,
        "ping_last_seen_seconds_ago": None,
        "mqtt_last_seen_seconds_ago": None,
        "ping_rtt_ms": None,
    }


def test_observe_records_each_source_independently() -> None:
    """Three different sources each fill their own slot; a fourth one is no-op."""
    with patch("time.monotonic") as monotonic:
        monotonic.return_value = 1000.0
        tracker = ReachabilityTracker()
        tracker.observe("kitchen", "mdns")
        monotonic.return_value = 1010.0
        tracker.observe("kitchen", "ping")
        monotonic.return_value = 1015.0
        tracker.observe("kitchen", "mqtt")
        # Unknown source is silently ignored — no exception, no map mutation.
        tracker.observe("kitchen", "garbage")

        monotonic.return_value = 1020.0
        snap = _snapshot(tracker)

    assert snap["mdns_last_seen_seconds_ago"] == 20.0
    assert snap["ping_last_seen_seconds_ago"] == 10.0
    assert snap["mqtt_last_seen_seconds_ago"] == 5.0


def test_observe_fires_callback_per_call() -> None:
    """Each tracked observation drives the subscriber notification."""
    seen: list[str] = []
    tracker = ReachabilityTracker(on_observation=seen.append)
    tracker.observe("kitchen", "mdns")
    tracker.observe("kitchen", "ping")
    tracker.observe("kitchen", "garbage")  # unmodelled → no fire

    assert seen == ["kitchen", "kitchen"]


def test_record_ping_rtt_sets_field_and_fires_callback() -> None:
    """``record_ping_rtt`` writes the rtt and notifies even without ``observe``."""
    seen: list[str] = []
    tracker = ReachabilityTracker(on_observation=seen.append)
    tracker.record_ping_rtt("kitchen", 4.2)

    snap = _snapshot(tracker)
    assert snap["ping_rtt_ms"] == 4.2
    # ``ping_last_seen`` is set by ``observe`` (in production: alongside
    # the RTT). RTT alone leaves the timestamp untouched so the
    # rendered "last seen" doesn't claim freshness from a stale ping.
    assert snap["ping_last_seen_seconds_ago"] is None
    assert seen == ["kitchen"]


def test_clear_removes_every_signal_for_a_device() -> None:
    """``clear`` is the mDNS-removed cleanup; idempotent on unknown names."""
    tracker = ReachabilityTracker()
    tracker.observe("kitchen", "mdns")
    tracker.observe("kitchen", "ping")
    tracker.observe("kitchen", "mqtt")
    tracker.record_ping_rtt("kitchen", 5.0)

    tracker.clear("kitchen")
    snap = _snapshot(tracker)
    assert snap["mdns_last_seen_seconds_ago"] is None
    assert snap["ping_last_seen_seconds_ago"] is None
    assert snap["mqtt_last_seen_seconds_ago"] is None
    assert snap["ping_rtt_ms"] is None

    # Clearing a never-tracked device is silent (no KeyError).
    tracker.clear("never-seen")


def test_clear_does_not_fire_observation_callback() -> None:
    """``clear`` is not a freshness signal — the subscriber stays quiet.

    Otherwise a removed device would push one final "you saw me!"
    snapshot to every open drawer, which contradicts the field
    semantics (clearing means we *stopped* seeing it).
    """
    seen: list[str] = []
    tracker = ReachabilityTracker(on_observation=seen.append)
    tracker.observe("kitchen", "mdns")
    seen.clear()

    tracker.clear("kitchen")
    assert seen == []


def test_snapshot_clamps_negative_relative_time_to_zero() -> None:
    """A clock skip that puts the timestamp ahead of ``now`` reads as ``0.0``.

    Without the clamp, a microsecond-level reordering between
    ``observe()`` capturing ``time.monotonic()`` and ``snapshot()``
    re-reading it on a different core surfaces as
    "-0.001 seconds ago" in the UI.
    """
    with patch("time.monotonic") as monotonic:
        monotonic.return_value = 1000.0
        tracker = ReachabilityTracker()
        tracker.observe("kitchen", "mdns")

        # Pretend the snapshot caller's clock is slightly *behind*
        # the observation's clock — clamp should pin to 0.
        monotonic.return_value = 999.999
        snap = _snapshot(tracker)

    assert snap["mdns_last_seen_seconds_ago"] == 0.0


def test_observations_isolated_per_device() -> None:
    """Two devices' freshness maps don't bleed into each other."""
    tracker = ReachabilityTracker()
    tracker.observe("kitchen", "mdns")
    tracker.observe("garage", "ping")

    kitchen = _snapshot(tracker, "kitchen", active_source="mdns", ip="10.0.0.42")
    garage = _snapshot(tracker, "garage", active_source="ping", ip="10.0.0.43")

    assert kitchen["mdns_last_seen_seconds_ago"] is not None
    assert kitchen["ping_last_seen_seconds_ago"] is None
    assert garage["mdns_last_seen_seconds_ago"] is None
    assert garage["ping_last_seen_seconds_ago"] is not None
