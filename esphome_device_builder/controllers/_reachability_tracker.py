"""
Per-signal freshness tracker for the device drawer's Reachability section.

The state monitor is the source of truth for "is the device online and via
which channel did we hear from it last." That decision boils down to a
single ``DeviceState`` and a single ``source``. The drawer wants more:
*every* channel's last-seen timestamp, independently, so the user can
see e.g. "mDNS heard 12s ago, ping answered 47s ago, MQTT silent for 8
min" in one glance.

This tracker owns the per-signal freshness:

- **mDNS** — read directly from zeroconf's DNS cache via the
  ``mdns_cache_reader`` callable. The cache's ``DNSAddress.created``
  timestamp is refreshed on every announce *we receive*, even when
  ``ServiceStateChange.Updated`` doesn't fire (zeroconf suppresses
  the callback for same-content TTL refreshes). Stamping at the
  callback site like we used to would lie about freshness — a
  60s-old announce that hasn't changed content would still read
  "5 min ago" because we never got a callback to bump our stamp.
  Reading ``created`` straight from the cache gives the truth.
- ``_ping_last_seen`` — set whenever an ICMP probe answers. Stamps
  are correct here because we directly receive the success.
- ``_mqtt_last_seen`` — set on every MQTT discovery payload routed
  through the state monitor. Same: direct receive.
- ``_ping_rtt_ms`` — paired with ``_ping_last_seen``; the most recent
  successful ping's round-trip in milliseconds.

The state monitor delegates: it calls :meth:`observe` on every
positive observation and :meth:`record_ping_rtt` after a successful
ping. The instance fires :attr:`on_observation` on every observation
(including mDNS) so subscribers (the drawer's per-device WS
subscription) can push a fresh snapshot to the UI without waiting
for a state transition. The push-trigger is separate from the
mDNS-age value: pushing tells the subscriber "look again," and
:meth:`snapshot` re-reads the cache to compute the *current* age.

Lives in its own module rather than as a few extra dicts inside
:class:`DeviceStateMonitor` so the state monitor stays focused on its
priority-rules + browser-callback + ping-loop work and the
reachability-display data lives somewhere a future caller can reuse
without inheriting the monitor's lifecycle.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..models import DeviceState

# Wire-format dict the drawer's ``devices/subscribe_reachability`` event
# carries. Defined as a TypedDict-style note rather than a runtime type
# so we don't pay for an extra dataclass — the dict is JSON-serialized
# by the WS layer either way.
ReachabilitySnapshot = dict[str, object]

# Callback fired every time we observe a freshness signal for a device,
# so the per-device subscription stream can push a refreshed snapshot.
ObservationCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class MdnsCacheInfo:
    """Truthful mDNS freshness derived from the zeroconf cache.

    ``age_seconds`` is the elapsed time since the device's most
    recent ``_esphomelib._tcp.local.`` SRV record was received,
    computed from :attr:`zeroconf.DNSAddress.created`. Refreshed
    on every announce zeroconf processes — including the same-
    content TTL refreshes that don't fire
    ``ServiceStateChange.Updated``.

    ``ttl_remaining_seconds`` is what
    :meth:`zeroconf.DNSAddress.get_remaining_ttl` reports — how
    long the cached entry has left before expiring without a
    fresh announce. The drawer surfaces it beside the row so the
    user can see whether the device is "due to re-announce" or
    "missed several windows already."
    """

    age_seconds: float
    ttl_remaining_seconds: float


# Reads the mDNS cache for a device name and returns the freshness
# info, or ``None`` when zeroconf isn't running / the device hasn't
# been heard from. Injected into the tracker rather than reaching for
# zeroconf directly so the tracker stays a pure data-shape and tests
# can pass a stub.
MdnsCacheReader = Callable[[str], MdnsCacheInfo | None]


class ReachabilityTracker:
    """Track per-signal last-seen timestamps and ping RTT per device."""

    def __init__(
        self,
        on_observation: ObservationCallback | None = None,
        mdns_cache_reader: MdnsCacheReader | None = None,
    ) -> None:
        self._on_observation = on_observation
        # Reads truthful mDNS freshness from zeroconf's cache. None
        # when the caller doesn't wire one (existing test fixtures);
        # the snapshot then falls through with ``None`` for the mDNS
        # fields, which the drawer renders as a hidden mDNS row.
        self._mdns_cache_reader = mdns_cache_reader
        # Each map keys on the device's ``esphome.name``. Values are
        # ``time.monotonic()`` seconds. We never compare the values
        # against absolute wall-clock; the snapshot subtracts them
        # against a fresh ``time.monotonic()`` to compute "N seconds
        # ago" so a clock skip can't make a 5s-ago observation look
        # 5 minutes old.
        #
        # Size is bounded by the configured-device count — each
        # observation overwrites its name's entry, never appends.
        # ``clear(name)`` is the only pruner and runs from two
        # paths: the mDNS browser's ``Removed`` event (broadcast
        # went away, in ``_device_state_monitor.py``) and
        # ``DevicesController._on_scan_change(REMOVED)`` (YAML
        # deleted). An OFFLINE state from ping or MQTT timeout
        # deliberately does *not* clear — the drawer still wants
        # to surface "we last heard on MQTT 8 minutes ago" so the
        # user can see when each channel went silent.
        #
        # Note: there is no ``_mdns_last_seen`` map. mDNS freshness
        # comes from the zeroconf cache via ``_mdns_cache_reader``;
        # stamping at the call site would lie when zeroconf
        # suppresses ``Updated`` callbacks for same-content TTL
        # refreshes (the cache still updates but our stamp would
        # not).
        self._ping_last_seen: dict[str, float] = {}
        self._mqtt_last_seen: dict[str, float] = {}
        self._ping_rtt_ms: dict[str, float] = {}

    def observe(self, name: str, source: str) -> None:
        """
        Notify subscribers of a fresh observation, stamping if applicable.

        For ``ping`` / ``mqtt``: stamps the per-source last-seen
        map. For ``mdns``: no stamp — freshness is read from the
        zeroconf cache by ``snapshot``. In every modelled case
        the observation callback fires so the drawer's
        per-device WS subscription can push a refreshed snapshot
        to the UI without waiting for the next state transition.

        Sources we don't model (``unknown``) are silent no-ops.
        """
        if source == "ping":
            self._ping_last_seen[name] = time.monotonic()
        elif source == "mqtt":
            self._mqtt_last_seen[name] = time.monotonic()
        elif source != "mdns":
            return
        if self._on_observation is not None:
            self._on_observation(name)

    def record_ping_rtt(self, name: str, rtt_ms: float) -> None:
        """
        Record the round-trip from a successful ICMP probe.

        Pure write — does *not* fire ``on_observation``. The state
        monitor's ping path always pairs this with ``apply(name,
        ONLINE, "ping")`` immediately after, which routes through
        ``observe(name, "ping")`` and fires the callback once.
        Firing here too would push two events for one ping. Future
        callers that want a notification should call ``observe()``
        explicitly after recording.
        """
        self._ping_rtt_ms[name] = rtt_ms

    def clear(self, name: str) -> None:
        """
        Drop every tracked signal for *name*.

        Used when mDNS reports the service ``Removed`` so the drawer
        doesn't show stale-by-hours timestamps after a re-announce.
        Idempotent — silently ignores names we've never tracked.
        The mDNS row clears automatically because zeroconf evicts
        the cache record alongside the ``Removed`` callback; this
        method only needs to clear the directly-tracked maps.
        """
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
        fields come from this tracker. mDNS reads the zeroconf
        cache live via ``_mdns_cache_reader``; ping / MQTT use
        ``time.monotonic()`` stamps from the directly-received
        observations. Times are clamped at zero — a tiny negative
        would only happen on clock skew, but a "-0.001s ago"
        display is jarring.

        Signals never observed for this device come through as
        ``None`` so the renderer can hide their row entirely.
        """
        now = time.monotonic()

        def _ago(timestamp: float | None) -> float | None:
            return None if timestamp is None else max(0.0, now - timestamp)

        mdns_age: float | None = None
        mdns_ttl_remaining: float | None = None
        if self._mdns_cache_reader is not None:
            info = self._mdns_cache_reader(name)
            if info is not None:
                mdns_age = info.age_seconds
                mdns_ttl_remaining = info.ttl_remaining_seconds

        return {
            "device": name,
            "state": state.value,
            "active_source": active_source,
            "ip": ip,
            "mdns_last_seen_seconds_ago": mdns_age,
            "mdns_ttl_remaining_seconds": mdns_ttl_remaining,
            "ping_last_seen_seconds_ago": _ago(self._ping_last_seen.get(name)),
            "mqtt_last_seen_seconds_ago": _ago(self._mqtt_last_seen.get(name)),
            "ping_rtt_ms": self._ping_rtt_ms.get(name),
        }
