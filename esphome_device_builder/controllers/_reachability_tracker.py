"""
Per-signal freshness tracker for the device drawer's Reachability section.

The state monitor owns the single ``DeviceState`` + active
source for a device. The drawer wants more: *every* channel's
last-seen independently so the user sees "mDNS heard 12s ago,
ping answered 47s ago, MQTT silent for 8 min" at one glance.

* **mDNS** age is read live from zeroconf's cache via
  ``mdns_cache_reader``, which folds the newest
  ``DNSRecord.created`` across the device's cached A / AAAA
  / SRV / TXT / PTR entries. Any of those refreshes on every
  announce *received*, including the same-content TTL
  refreshes that suppress ``ServiceStateChange.Updated`` (so
  stamping at the callback site would lie).
* **ping** / **MQTT** last-seen are stamped on the direct
  observation; both signals fire callbacks every time.
* **ping_rtt_ms** is paired with the most recent successful
  ping.

The state monitor calls :meth:`observe` on every positive
observation; the tracker fires :attr:`on_observation` so the
drawer's per-device subscription can push a fresh snapshot
without waiting for a state transition.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..models import DeviceReachabilityData, DeviceState

# Wire-shape alias kept local; ``DeviceReachabilityData`` is
# the canonical name at fire / listener sites.
ReachabilitySnapshot = DeviceReachabilityData

# Fired on every freshness signal so the per-device
# subscription stream can push a refreshed snapshot.
ObservationCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class MdnsCacheInfo:
    """
    Truthful mDNS freshness derived from the zeroconf cache.

    ``age_seconds`` = elapsed time since the newest cached
    :class:`zeroconf.DNSRecord` for the device, folding the
    A / AAAA / SRV / TXT / PTR entries together so any
    refresh counts.
    ``ttl_remaining_seconds`` = the same record's
    :meth:`DNSRecord.get_remaining_ttl`.
    ``ptr_ttl_remaining_seconds`` = the PTR record's own
    remaining TTL, i.e. seconds until ``AsyncServiceBrowser``
    fires ``Removed`` and the device flips OFFLINE; ``None``
    when no PTR is cached. Distinct from the union TTL above,
    which tracks whichever record is freshest (usually the
    ~120s A record the refresh loop renews).
    ``txt_records`` = parsed ``key -> value`` pairs from the
    device's TXT record, sorted alphabetically for
    deterministic wire output.

    Bare keys (``foo`` with no ``=``), empty-value entries,
    and UTF-8-decode failures all surface as ``""``-valued
    keys — same empty-string-means-confirmed signal the
    upstream ``api_encryption`` tri-state uses (#437). Keys
    that fail UTF-8 decode are dropped. Empty ``txt_records``
    when no TXT record is cached.
    """

    age_seconds: float
    ttl_remaining_seconds: float
    ptr_ttl_remaining_seconds: float | None = None
    txt_records: dict[str, str] = field(default_factory=dict)


# Reads the mDNS cache for a device name; ``None`` when
# zeroconf isn't running / the device hasn't been heard.
# Injected so tests can pass a stub.
MdnsCacheReader = Callable[[str], MdnsCacheInfo | None]


class ReachabilityTracker:
    """Track per-signal last-seen timestamps and ping RTT per device."""

    def __init__(
        self,
        on_observation: ObservationCallback | None = None,
        mdns_cache_reader: MdnsCacheReader | None = None,
    ) -> None:
        self._on_observation = on_observation
        self._mdns_cache_reader = mdns_cache_reader
        # Each map keys on ``esphome.name`` and holds
        # ``time.monotonic()`` seconds (subtracted against a
        # fresh ``time.monotonic()`` at snapshot time, so a
        # wall-clock skip can't age observations).
        #
        # No ``_mdns_last_seen`` — mDNS freshness comes from
        # the cache reader; stamping at the call site lies
        # when zeroconf suppresses ``Updated`` callbacks for
        # same-content TTL refreshes.
        #
        # ``clear(name)`` is the only pruner (mDNS browser's
        # ``Removed`` + ``DevicesController`` YAML-delete). An
        # OFFLINE state from ping / MQTT timeout deliberately
        # does *not* clear — the drawer wants "we last heard
        # on MQTT 8 min ago" after the channel goes silent.
        self._ping_last_seen: dict[str, float] = {}
        self._mqtt_last_seen: dict[str, float] = {}
        self._ping_rtt_ms: dict[str, float] = {}

    def observe(self, name: str, source: str) -> None:
        """
        Notify subscribers of a fresh observation, stamping if applicable.

        ``ping`` / ``mqtt`` stamp the per-source last-seen
        map. ``mdns`` doesn't stamp (freshness is read live
        from the zeroconf cache by :meth:`snapshot`) but
        still fires the callback. Other sources are silent
        no-ops.
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
        Record a successful ICMP probe's round-trip.

        Pure write — does *not* fire ``on_observation``. The
        state monitor's ping path always pairs this with an
        ``observe(name, "ping")`` that fires the callback;
        firing here would push two events for one ping.
        """
        self._ping_rtt_ms[name] = rtt_ms

    def clear(self, name: str) -> None:
        """
        Drop every tracked signal for *name*.

        Called when mDNS reports ``Removed`` or the YAML is
        deleted. Idempotent. The mDNS row clears itself —
        zeroconf evicts the cache record alongside ``Removed``.
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

        ``state`` / ``active_source`` / ``ip`` come from the
        state monitor; freshness fields come from this
        tracker. mDNS reads the zeroconf cache live; ping /
        MQTT use ``time.monotonic()`` stamps. Ages are clamped
        at zero. Never-observed signals come through as
        ``None`` so the renderer can hide the row.
        """
        now = time.monotonic()

        def _ago(timestamp: float | None) -> float | None:
            return None if timestamp is None else max(0.0, now - timestamp)

        mdns_age: float | None = None
        mdns_ttl_remaining: float | None = None
        mdns_ptr_ttl_remaining: float | None = None
        # ``None`` means "hide the TXT section" — collapses
        # both "no TXT cached" and "TXT cached but no useful
        # keys decoded" so the renderer is a single
        # null-check.
        mdns_txt_records: dict[str, str] | None = None
        if self._mdns_cache_reader is not None:
            info = self._mdns_cache_reader(name)
            if info is not None:
                mdns_age = info.age_seconds
                mdns_ttl_remaining = info.ttl_remaining_seconds
                mdns_ptr_ttl_remaining = info.ptr_ttl_remaining_seconds
                # Fresh dict on the wire so downstream mutation
                # can't reach into zeroconf's internals.
                mdns_txt_records = dict(info.txt_records) if info.txt_records else None

        return {
            "device": name,
            "state": state.value,
            "active_source": active_source,
            "ip": ip,
            "mdns_last_seen_seconds_ago": mdns_age,
            "mdns_ttl_remaining_seconds": mdns_ttl_remaining,
            "mdns_ptr_ttl_remaining_seconds": mdns_ptr_ttl_remaining,
            "mdns_txt_records": mdns_txt_records,
            "ping_last_seen_seconds_ago": _ago(self._ping_last_seen.get(name)),
            "mqtt_last_seen_seconds_ago": _ago(self._mqtt_last_seen.get(name)),
            "ping_rtt_ms": self._ping_rtt_ms.get(name),
        }
