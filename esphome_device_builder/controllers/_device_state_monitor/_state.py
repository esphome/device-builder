"""Mutable domain state for :class:`DeviceStateMonitor`."""

from __future__ import annotations

from dataclasses import dataclass, field

from .._dns_cache import DNSCache
from .._reachability_tracker import ReachabilityTracker


@dataclass
class MonitorState:
    """Mutable state for :class:`DeviceStateMonitor`."""

    # Source-precedence ledger: device name → ``"mdns"`` /
    # ``"mqtt"`` / ``"ping"``. Only one source can own a
    # device's state at a time; mDNS always wins, ping only
    # writes when mDNS hasn't already resolved the device.
    # Every ``apply_*`` path consults this to gate writes.
    state_source: dict[str, str] = field(default_factory=dict)

    # Device name → web-UI URL discovered via the
    # ``_http._tcp.local.`` browser. Populated by the
    # importable-discovery flow; rendered on the
    # discovered-device card so the frontend can show a
    # Visit-web-UI link without knowing which factory
    # firmwares ship a web server.
    http_urls: dict[str, str] = field(default_factory=dict)

    # DNS resolutions for non-mDNS hostnames, cached with TTLs
    # so the ping sweep, OTA cache args, and ``device.ip``
    # tracking all share the same lookup result instead of
    # re-resolving every cycle.
    dns_cache: DNSCache = field(default_factory=DNSCache)

    # Per-signal freshness tracker (mDNS / ping / MQTT
    # last-seen, ping RTT). Optional dependency: callers
    # that don't care about reachability metadata pass
    # ``None`` and the monitor's observation hooks become
    # no-ops.
    reachability: ReachabilityTracker | None = None
