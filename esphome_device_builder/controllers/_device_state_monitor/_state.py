"""Mutable domain state for :class:`DeviceStateMonitor`."""

from __future__ import annotations

from dataclasses import dataclass, field

from .._dns_cache import DNSCache
from .._reachability_tracker import ReachabilityTracker


@dataclass
class MonitorState:
    """Mutable state for :class:`DeviceStateMonitor`."""

    # Source-precedence ledger: device name → ``"mdns"`` /
    # ``"mqtt"`` / ``"ping"``. The ``apply`` write path gates
    # observations on this so a lower-priority source can't
    # clobber an mDNS-owned state.
    state_source: dict[str, str] = field(default_factory=dict)

    # Device name → web-UI URL from the ``_http._tcp.local.``
    # browser. Populated by the importable-discovery flow,
    # read when building each ``AdoptableDevice``.
    http_urls: dict[str, str] = field(default_factory=dict)

    # TTL'd DNS cache shared across the ping sweep, OTA cache
    # args, and ``device.ip`` tracking so the three paths agree
    # on the resolved IP without re-resolving each cycle.
    dns_cache: DNSCache = field(default_factory=DNSCache)

    # Optional per-signal freshness tracker (mDNS / ping / MQTT
    # last-seen, ping RTT). ``None`` makes the monitor's
    # observation hooks no-ops — kept for tests that don't wire
    # the drawer.
    reachability: ReachabilityTracker | None = None
