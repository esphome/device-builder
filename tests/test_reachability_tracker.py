"""
Unit coverage for :class:`ReachabilityTracker`.

The tracker mixes two freshness sources:

* ``ping`` / ``mqtt`` — direct stamps in ``time.monotonic()`` dicts.
* ``mdns`` — read via the injected ``mdns_cache_reader`` from
  zeroconf's actual cache. Tests pass a fake reader rather than
  spinning up zeroconf, so we can drive the (age, ttl_remaining)
  return values directly.

We patch ``time.monotonic`` rather than relying on real wall-clock so
the relative-time assertions can be exact (no flakes from a busy CI
runner).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from esphome_device_builder.controllers._reachability_tracker import (
    MdnsCacheInfo,
    ReachabilityTracker,
)
from esphome_device_builder.models import DeviceState

# Tests pass ``dict.get`` directly as the ``MdnsCacheReader`` —
# bound-method already matches the ``Callable[[str], MdnsCacheInfo
# | None]`` shape, so no wrapper factory is needed.


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
        "mdns_ttl_remaining_seconds": None,
        "mdns_ptr_ttl_remaining_seconds": None,
        "mdns_txt_records": None,
        "ping_last_seen_seconds_ago": None,
        "mqtt_last_seen_seconds_ago": None,
        "ping_rtt_ms": None,
    }


def test_snapshot_uses_mdns_cache_reader() -> None:
    """MDNS freshness comes from the injected cache reader, not from ``observe``.

    ``observe(name, "mdns")`` deliberately does NOT stamp a value
    (zeroconf can suppress ``Updated`` callbacks for same-content
    TTL refreshes — stamping at the call site would lie). The
    snapshot reads truth from the cache reader.
    """
    info = MdnsCacheInfo(
        age_seconds=12.4,
        ttl_remaining_seconds=107.6,
        ptr_ttl_remaining_seconds=4321.0,
    )
    tracker = ReachabilityTracker(
        mdns_cache_reader={"kitchen": info}.get,
    )

    snap = _snapshot(tracker)
    assert snap["mdns_last_seen_seconds_ago"] == 12.4
    assert snap["mdns_ttl_remaining_seconds"] == 107.6
    # The PTR's own TTL — the drawer's "offline in N" countdown —
    # is carried separately from the freshest-record union TTL.
    assert snap["mdns_ptr_ttl_remaining_seconds"] == 4321.0


def test_snapshot_mdns_null_when_cache_reader_returns_none() -> None:
    """No cache entry → null mDNS fields, drawer hides the row."""
    tracker = ReachabilityTracker(mdns_cache_reader={}.get)

    snap = _snapshot(tracker)
    assert snap["mdns_last_seen_seconds_ago"] is None
    assert snap["mdns_ttl_remaining_seconds"] is None
    assert snap["mdns_ptr_ttl_remaining_seconds"] is None
    assert snap["mdns_txt_records"] is None


def test_snapshot_includes_txt_records_when_present() -> None:
    """
    TXT records on the wire let the drawer show a debug collapsible.

    Pin the wire shape: a non-empty ``txt_records`` mapping on the
    ``MdnsCacheInfo`` round-trips through the snapshot dict as a
    fresh ``dict[str, str]``, so a downstream caller mutating the
    serialised dict can't poke back into the cache.
    """
    info = MdnsCacheInfo(
        age_seconds=1.0,
        ttl_remaining_seconds=119.0,
        txt_records={
            "version": "2025.4.0",
            "config_hash": "5a94a12d",
            "mac": "aabbccddeeff",
        },
    )
    tracker = ReachabilityTracker(mdns_cache_reader={"kitchen": info}.get)

    snap = _snapshot(tracker)
    assert snap["mdns_txt_records"] == {
        "version": "2025.4.0",
        "config_hash": "5a94a12d",
        "mac": "aabbccddeeff",
    }
    # Wire-side dict is a copy: mutating it doesn't reach the
    # cached info object the next snapshot will read.
    snap["mdns_txt_records"]["version"] = "tampered"  # type: ignore[index]
    assert info.txt_records["version"] == "2025.4.0"


def test_snapshot_drops_empty_txt_records_to_none() -> None:
    """Empty ``txt_records`` → ``None`` on the wire so the drawer hides the debug section.

    A collapsible header with zero rows is just visual noise; the
    ``None`` distinction lets the renderer skip rendering the
    section entirely.
    """
    info = MdnsCacheInfo(age_seconds=1.0, ttl_remaining_seconds=119.0, txt_records={})
    tracker = ReachabilityTracker(mdns_cache_reader={"kitchen": info}.get)

    snap = _snapshot(tracker)
    assert snap["mdns_last_seen_seconds_ago"] == 1.0
    assert snap["mdns_txt_records"] is None


def test_observe_records_ping_and_mqtt_stamps() -> None:
    """Ping / mqtt observe stamps the per-source dict; mdns does not."""
    with patch("time.monotonic") as monotonic:
        monotonic.return_value = 1000.0
        tracker = ReachabilityTracker()
        tracker.observe("kitchen", "mdns")  # no stamp — cache-only
        monotonic.return_value = 1010.0
        tracker.observe("kitchen", "ping")
        monotonic.return_value = 1015.0
        tracker.observe("kitchen", "mqtt")
        # Unknown source is silently ignored — no exception, no map mutation.
        tracker.observe("kitchen", "garbage")

        monotonic.return_value = 1020.0
        snap = _snapshot(tracker)

    # mDNS has no stamp; the cache reader wasn't injected so it
    # comes through as None — exactly the "no truth available"
    # signal the drawer needs to hide the row.
    assert snap["mdns_last_seen_seconds_ago"] is None
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


def test_record_ping_rtt_sets_field_without_firing_callback() -> None:
    """``record_ping_rtt`` is a pure write — does NOT fire ``on_observation``.

    The state monitor's ping path always pairs ``record_ping_rtt``
    with ``apply(name, ONLINE, "ping")`` (which routes through
    ``observe`` and fires the callback once). Firing here too
    would push two events for one ping — wasted bus traffic. Pin
    the contract so a future change that adds a redundant fire
    here gets flagged.
    """
    seen: list[str] = []
    tracker = ReachabilityTracker(on_observation=seen.append)
    tracker.record_ping_rtt("kitchen", 4.2)

    snap = _snapshot(tracker)
    assert snap["ping_rtt_ms"] == 4.2
    # ``ping_last_seen`` is set by ``observe`` (in production: alongside
    # the RTT). RTT alone leaves the timestamp untouched so the
    # rendered "last seen" doesn't claim freshness from a stale ping.
    assert snap["ping_last_seen_seconds_ago"] is None
    # No notification — the paired ``observe`` is what fires.
    assert seen == []


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
    "-0.001 seconds ago" in the UI. mDNS is cache-driven so
    can't hit this race; ping / MQTT use the stamp path.
    """
    with patch("time.monotonic") as monotonic:
        monotonic.return_value = 1000.0
        tracker = ReachabilityTracker()
        tracker.observe("kitchen", "ping")

        # Pretend the snapshot caller's clock is slightly *behind*
        # the observation's clock — clamp should pin to 0.
        monotonic.return_value = 999.999
        snap = _snapshot(tracker)

    assert snap["ping_last_seen_seconds_ago"] == 0.0


def test_observations_isolated_per_device() -> None:
    """Two devices' freshness maps don't bleed into each other."""
    info = MdnsCacheInfo(age_seconds=3.0, ttl_remaining_seconds=117.0)
    tracker = ReachabilityTracker(mdns_cache_reader={"kitchen": info}.get)
    tracker.observe("garage", "ping")

    kitchen = _snapshot(tracker, "kitchen", active_source="mdns", ip="10.0.0.42")
    garage = _snapshot(tracker, "garage", active_source="ping", ip="10.0.0.43")

    assert kitchen["mdns_last_seen_seconds_ago"] == 3.0
    assert kitchen["ping_last_seen_seconds_ago"] is None
    assert garage["mdns_last_seen_seconds_ago"] is None
    assert garage["ping_last_seen_seconds_ago"] is not None
