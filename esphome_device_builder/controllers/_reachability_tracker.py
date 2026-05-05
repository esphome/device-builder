"""
Per-signal freshness tracker for the device drawer's Reachability section.

The state monitor is the source of truth for "is the device online and via
which channel did we hear from it last." That decision boils down to a
single ``DeviceState`` and a single ``source``. The drawer wants more:
*every* channel's last-seen timestamp, independently, so the user can
see e.g. "mDNS heard 12s ago, ping answered 47s ago, MQTT silent for 8
min" in one glance.

This tracker owns the four maps that supply the answer:

- ``_mdns_last_seen`` — set on every mDNS service ``Added`` / ``Updated``
  event and on every successful active resolve.
- ``_ping_last_seen`` — set whenever an ICMP probe answers.
- ``_mqtt_last_seen`` — set on every MQTT discovery payload routed
  through the state monitor.
- ``_ping_rtt_ms`` — paired with ``_ping_last_seen``; the most recent
  successful ping's round-trip in milliseconds.

The state monitor delegates: it calls :meth:`observe` on every
positive observation and :meth:`record_ping_rtt` after a successful
ping. The instance fires :attr:`on_observation` on every record so
subscribers (the drawer's per-device WS subscription) can push a
fresh snapshot to the UI without waiting for a state transition.

Lives in its own module rather than as a few extra dicts inside
:class:`DeviceStateMonitor` so the state monitor stays focused on its
priority-rules + browser-callback + ping-loop work and the
reachability-display data lives somewhere a future caller can reuse
without inheriting the monitor's lifecycle.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from ..models import DeviceState

# Wire-format dict the drawer's ``subscribe_device_reachability`` event
# carries. Defined as a TypedDict-style note rather than a runtime type
# so we don't pay for an extra dataclass — the dict is JSON-serialized
# by the WS layer either way.
ReachabilitySnapshot = dict[str, object]

# Callback fired every time we observe a freshness signal for a device,
# so the per-device subscription stream can push a refreshed snapshot.
ObservationCallback = Callable[[str], None]


class ReachabilityTracker:
    """Track per-signal last-seen timestamps and ping RTT per device."""

    def __init__(self, on_observation: ObservationCallback | None = None) -> None:
        self._on_observation = on_observation
        # Each map keys on the device's ``esphome.name``. Values are
        # ``time.monotonic()`` seconds. We never compare the values
        # against absolute wall-clock; the snapshot subtracts them
        # against a fresh ``time.monotonic()`` to compute "N seconds
        # ago" so a clock skip can't make a 5s-ago observation look
        # 5 minutes old.
        self._mdns_last_seen: dict[str, float] = {}
        self._ping_last_seen: dict[str, float] = {}
        self._mqtt_last_seen: dict[str, float] = {}
        self._ping_rtt_ms: dict[str, float] = {}

    def observe(self, name: str, source: str) -> None:
        """
        Stamp *source*'s last-seen for *name* and notify any subscriber.

        Sources we don't model (``unknown``) are no-ops — the caller
        gates on ``DeviceState.ONLINE`` so an OFFLINE-from-mdns
        observation doesn't bump mDNS freshness, but it doesn't have
        to gate on the source enum since we silently ignore anything
        unfamiliar.
        """
        now = time.monotonic()
        if source == "mdns":
            self._mdns_last_seen[name] = now
        elif source == "ping":
            self._ping_last_seen[name] = now
        elif source == "mqtt":
            self._mqtt_last_seen[name] = now
        else:
            return
        if self._on_observation is not None:
            self._on_observation(name)

    def record_ping_rtt(self, name: str, rtt_ms: float) -> None:
        """
        Record the round-trip from a successful ICMP probe.

        Called from the state monitor's ping path. The accompanying
        ``observe(name, "ping")`` is fired separately by the apply
        flow when the new state is ONLINE, so callers don't need to
        thread "did we already observe?" logic — just record the rtt
        when icmplib hands one back. Notification fires here too so
        an RTT-only update (rare; same ONLINE state, fresh number)
        still pushes to the subscriber.
        """
        self._ping_rtt_ms[name] = rtt_ms
        if self._on_observation is not None:
            self._on_observation(name)

    def clear(self, name: str) -> None:
        """
        Drop every tracked signal for *name*.

        Used when mDNS reports the service ``Removed`` so the drawer
        doesn't show stale-by-hours timestamps after a re-announce.
        Idempotent — silently ignores names we've never tracked.
        """
        self._mdns_last_seen.pop(name, None)
        self._ping_last_seen.pop(name, None)
        self._mqtt_last_seen.pop(name, None)
        self._ping_rtt_ms.pop(name, None)

    def snapshot(
        self,
        name: str,
        *,
        state: DeviceState,
        active_source: str,
        ip: str,
    ) -> ReachabilitySnapshot:
        """
        Return the wire-shape dict for the per-device subscription.

        ``state`` / ``active_source`` / ``ip`` come from the state
        monitor (it's the source of truth for those); the freshness
        fields come from this tracker. ``*_seconds_ago`` are computed
        against a fresh ``time.monotonic()`` so the drawer can render
        "N seconds ago" without trusting the backend's clock to
        match its own.

        Signals never observed for this device come through as
        ``None`` so the renderer can hide their row entirely. Times
        are clamped at zero — a tiny negative would only happen on
        clock skew, but a "-0.001s ago" display is jarring.
        """
        now = time.monotonic()

        def _ago(timestamp: float | None) -> float | None:
            return None if timestamp is None else max(0.0, now - timestamp)

        return {
            "device": name,
            "state": state.value,
            "active_source": active_source,
            "ip": ip,
            "mdns_last_seen_seconds_ago": _ago(self._mdns_last_seen.get(name)),
            "ping_last_seen_seconds_ago": _ago(self._ping_last_seen.get(name)),
            "mqtt_last_seen_seconds_ago": _ago(self._mqtt_last_seen.get(name)),
            "ping_rtt_ms": self._ping_rtt_ms.get(name),
        }
