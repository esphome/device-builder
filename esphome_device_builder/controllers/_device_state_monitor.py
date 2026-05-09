"""
Device connectivity monitor — mDNS browser + ping fallback.

Tracks online/offline state for the configured devices, with mDNS as
the primary source (event-driven) and ICMP ping as a periodic fallback
for devices that aren't broadcasting their service. MQTT observations
are also welcomed via :meth:`apply` for devices that opt into MQTT
discovery. The monitor calls back into the owning controller whenever
a state actually changes; controllers stay free of zeroconf / icmplib
/ aiomqtt details.

Source precedence (highest first): ``mdns`` > ``mqtt`` > ``ping``. A
lower-priority source can never override the state set by a higher one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from functools import lru_cache
from operator import attrgetter
from typing import Any

from esphome.zeroconf import (
    AsyncEsphomeZeroconf,
    DashboardImportDiscovery,
    DiscoveredImport,
)
from zeroconf import AddressResolver, IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

try:
    from icmplib import async_ping as icmp_ping
except ImportError:  # pragma: no cover — icmplib is optional
    icmp_ping = None  # type: ignore[assignment]

from zeroconf import current_time_millis, millis_to_seconds
from zeroconf.const import (
    _CLASS_IN,
    _TYPE_A,
    _TYPE_AAAA,
    _TYPE_SRV,
    _TYPE_TXT,
)

from ..helpers.hostname import is_local_hostname, normalize_hostname
from ..helpers.subscriber_presence import SubscriberPresence
from ..models import AdoptableDevice, Device, DeviceState, ReachabilitySource
from ._dns_cache import DNSCache
from ._reachability_tracker import MdnsCacheInfo, ReachabilityTracker

_LOGGER = logging.getLogger(__name__)
_ESPHOME_SERVICE_TYPE = "_esphomelib._tcp.local."
# A second mDNS browser watches for HTTP services so we can light up
# a "Visit web UI" link on discovered devices that are running their
# factory firmware's built-in web server. The browser only feeds the
# importable-discovery flow; configured devices already get their
# web_port from the YAML (``web_server:``).
_HTTP_SERVICE_TYPE = "_http._tcp.local."
# Ping fallback runs every 60s after a short bootstrap window.
# ``_PING_BOOTSTRAP_DELAY`` gives the mDNS browser a head start so the
# common case (everything announces) doesn't fire a ping sweep that
# the browser would have answered for free a few seconds later. 10s
# tracks the upstream esphome dashboard's ``MDNS_BOOTSTRAP_TIME``
# (~7.5s) closely enough to stay correct without making the user wait
# a full minute to see UNKNOWN devices flip OFFLINE on first load.
_PING_INTERVAL = 60  # seconds between ping sweeps
_PING_BOOTSTRAP_DELAY = 10  # seconds before the first ping sweep
# Batch size matches the upstream esphome dashboard's
# ``GROUP_SIZE = MAX_EXECUTOR_WORKERS / 2 = 24``. Each batch's pings
# run in parallel via ``asyncio.gather``; the cap exists because
# icmplib gets unreliable past a few dozen concurrent probes. With a
# small fleet (≤24 ping candidates) one batch covers everything and
# the sweep finishes in a single ICMP timeout window instead of
# stacking N timeouts back-to-back.
_PING_BATCH_SIZE = 24
_MDNS_RESOLVE_TIMEOUT_MS = 2000
# Padding added to the cached A record's TTL when the drawer's
# refresh loop schedules its next probe. We sleep ``ttl + this``
# so by the time we wake up the cache record has aged past
# expiry, ``_load_from_cache`` short-circuits fail, and
# ``async_resolve_host`` actually goes on the wire (it
# short-circuits otherwise). Keep small — extra padding is just
# a window where the drawer's mDNS row reads "Waiting for first
# broadcast…" between the record's natural expiry and our
# scheduled wake-up.
_MDNS_REFRESH_PADDING_SECONDS = 1.0
# Timeout for the per-sweep mDNS hostname resolves we issue for
# non-API devices. 3s is enough on a working LAN even when the
# device is briefly slow to respond, and keeps the whole resolve
# pass under the ping interval even if every target misses the
# cache and has to round-trip on the network.
_MDNS_HOSTNAME_RESOLVE_TIMEOUT = 3.0

# Source priority for state observations. A new observation can only
# override an existing one when its priority is greater than or equal
# to the current source's. Keep ``unknown`` at zero so any source can
# claim a device that no source has yet labelled.
_SOURCE_PRIORITY: dict[str, int] = {
    ReachabilitySource.UNKNOWN: 0,
    ReachabilitySource.PING: 1,
    ReachabilitySource.MQTT: 2,
    ReachabilitySource.MDNS: 3,
}


# Allowed separators between the six octets of a MAC.
# ESPHome firmware today broadcasts the compact 12-hex-char form
# (no separators); the dashboard's *canonical* form
# (``XX:XX:XX:XX:XX:XX``, applied at ingest by ``_normalize_mac``)
# uses ``:``. We normalize away ``-`` (Windows-style) and ``.``
# (Cisco) too so a future firmware change or vendored tool can't
# slip a non-canonical form into the dedupe path or the sidecar.
_MAC_SEPARATORS = str.maketrans("", "", ":-.")


def _normalize_mac(value: str) -> str:
    """Canonicalise a broadcast MAC to ``XX:XX:XX:XX:XX:XX`` form.

    Strips ``:`` / ``-`` / ``.`` separators, uppercases, validates
    the result is 12 hex chars, then re-inserts ``:`` between every
    octet. Returns ``""`` when the input doesn't shape into a
    48-bit hex MAC — callers treat that the same as "TXT absent"
    and skip the apply path. Done at ingest so the dedupe, sidecar,
    in-memory model, and frontend wire all carry one canonical form
    regardless of which case / separator style the firmware happens
    to broadcast.
    """
    stripped = value.translate(_MAC_SEPARATORS).upper()
    if len(stripped) != 12:
        return ""
    try:
        int(stripped, 16)
    except ValueError:
        return ""
    return ":".join(stripped[i : i + 2] for i in range(0, 12, 2))


# Callback signature used by DeviceStateMonitor to push state changes
# back to its owner. The owner decides what to do with the new state
# (e.g. fire a bus event, mutate the device model).
StateChangeCallback = Callable[[str, DeviceState, str], None]

# Callback fired when mDNS resolves (or clears) a device's IP address.
# ``primary`` is the IPv4 we lock onto for ICMP / OTA cache args (or
# the first scoped IPv6 when a host has no V4); ``addresses`` is the
# announced set — order is whatever zeroconf's
# ``parsed_scoped_addresses(IPVersion.All)`` returned (in practice
# IPv4 first, then any scoped IPv6 entries). Single-IP sources (MQTT,
# DNS fallback) carry just the one address they know. Empty primary +
# empty list signals the device went offline / was removed from mDNS.
IPChangeCallback = Callable[[str, str, list[str]], None]

# Callback fired when the mDNS ``version`` TXT record reports a
# different firmware version than last seen for a device.
VersionChangeCallback = Callable[[str, str], None]

# Callback fired when the mDNS ``config_hash`` TXT record reports a
# different running-config hash than last seen for a device. The hash
# is the 8-char lowercase hex of ``App.get_config_hash()`` and is only
# broadcast by firmware built from esphome/esphome#16145 onwards;
# older devices simply never fire this callback.
ConfigHashChangeCallback = Callable[[str, str], None]

# Callback fired when the mDNS ``api_encryption`` TXT record reports a
# different value than last seen.
#
#   * Non-empty (e.g. ``Noise_NNpsk0_25519_ChaChaPoly_SHA256``) →
#     encryption confirmed live on the device.
#   * Empty string → device is explicitly broadcasting plaintext.
#     Two wire shapes land here: TXT carrying the
#     ``api_encryption`` key with an empty / bare value (zeroconf
#     collapses both to ``None`` and the apply path normalises to
#     ``""``), AND a content-bearing TXT that omits the key
#     entirely (firmware was rebuilt without encryption — the
#     omission inside an otherwise-populated announce is
#     authoritative for "encryption was removed").
#
# The "no signal" case (no mDNS seen, or a truly empty re-announce
# with no other TXT keys) never fires this callback — the apply
# path at ``_apply_service_info`` only treats key-absence as
# authoritative when the announce carried other content. The
# device controller keeps that state as ``None`` to mean "trust
# whatever the YAML says".
ApiEncryptionChangeCallback = Callable[[str, str], None]

# Callback fired when the mDNS ``mac`` TXT record reports a different
# MAC than last seen for a device. The value passed has already been
# normalized by :func:`_normalize_mac` to the canonical
# ``XX:XX:XX:XX:XX:XX`` form (uppercase, colon-separated); the
# frontend renders it directly with no per-display formatter. Empty /
# missing TXT skips the callback — devices on firmware predating the
# ``mac`` broadcast stay with whatever value they already had.
MacAddressChangeCallback = Callable[[str, str], None]

# Callback fired when zeroconf turns up a previously-unseen device that
# advertises ``package_import_url`` / ``project_name`` /
# ``project_version`` TXT records — the signal that this is a factory
# build ready to be adopted into the dashboard. The companion
# ``ImportableRemovedCallback`` fires when the service goes away.
ImportableAddedCallback = Callable[[AdoptableDevice], None]
ImportableRemovedCallback = Callable[[str], None]


def _http_url_from_service_info(device_name: str, info: AsyncServiceInfo) -> str:
    """Build ``http://<host>[:port]`` from a populated HTTP service info.

    Single source of truth for the URL shape — ``_apply_http_service_info``
    (browser callback path) and ``_seed_http_url_from_cache`` (late-binding
    path when the HTTP service was already cached before the importable
    arrived) both call this so the format stays consistent.

    ``info.server`` is trusted only when it's an ``.local`` hostname.
    Anything else (a routable hostname, a remote SRV target) gets
    rewritten to ``<device_name>.local`` so a malicious or
    misconfigured announcement can't surface a clickable link
    pointing somewhere off-LAN.
    """
    raw_server = info.server.removesuffix(".") if info.server else ""
    host = raw_server if is_local_hostname(raw_server) else f"{device_name}.local"
    port = info.port or 80
    return f"http://{host}{'' if port == 80 else f':{port}'}"


@lru_cache(maxsize=256)
def _decode_txt_bytes_to_sorted_pairs(txt_bytes: bytes) -> tuple[tuple[str, str], ...]:
    """
    Bytes-keyed memoised TXT decode — the reusable hot path.

    The reachability snapshot fires on every observation; for a
    50-device fleet with the drawer open that's ~50 calls/sec
    against a ``DNSText`` cache where each device's TXT bytes
    rarely change between firmware flashes. The decode itself
    (``ServiceInfo.text`` setter → ``decoded_properties`` →
    sort + filter) costs about an allocation-heavy 50µs per call;
    keying on the immutable raw bytes turns 49 of every 50 of
    those calls into hash-table lookups.

    Returns an immutable ``tuple[tuple[str, str], ...]`` rather
    than a dict so a downstream caller mutating the result can't
    poison subsequent cache hits. Callers materialise a fresh
    dict via ``dict(pairs)``.

    Bytes are content-addressed: two devices broadcasting
    byte-identical TXT payloads share a cache entry, which is
    correct (the decoded output is the same).

    ``maxsize=256`` covers fleet sizes well past the typical
    tens-to-low-hundreds, with headroom for a device that
    re-broadcasts a slightly different TXT payload (firmware
    upgrade, ``config_hash`` change) without immediately
    evicting another device's stable entry. Still bounded so a
    long-running dashboard with rotating device names can't grow
    the cache without limit.
    """
    # ``service_name`` is required by the ctor but doesn't affect
    # ``set_text`` parsing — pass a placeholder so the cache key
    # stays bytes-only.
    info = AsyncServiceInfo(_ESPHOME_SERVICE_TYPE, f"_decode.{_ESPHOME_SERVICE_TYPE}")
    info.text = txt_bytes
    decoded = info.decoded_properties
    return tuple(
        (key, decoded[key] if isinstance(decoded[key], str) else "")
        for key in sorted(decoded)
        if isinstance(key, str)
    )


def _decode_mdns_txt_records(txt_dns_records: list[Any]) -> dict[str, str]:
    """
    Decode the freshest cached ``DNSText`` record into a sorted ``key=value`` dict.

    Reuses ``ServiceInfo.text`` setter so we get zeroconf's canonical
    RFC-6763 split (length-prefixed UTF-8 entries → ``key=value``
    pairs) and ``decoded_properties`` for the UTF-8 decode +
    bad-bytes-to-``None`` handling. Skips ``load_from_cache`` so
    tests can stub the cache with a ``MagicMock``: that helper's
    strict ``DNSCache`` isinstance check would crash the test path,
    and the only thing we need from the cache here is the
    already-fetched TXT bytes.

    Empty / bare-key handling: zeroconf collapses both bare keys
    (``foo`` with no ``=``) and empty-value entries (``foo=`` with
    ``=`` but no value) to the same ``None`` in
    ``decoded_properties``. The diagnostic value is the same in
    both cases — the user wants to see that the key IS present
    even if the value is empty — so we surface those as ``""``
    rather than dropping them. The empty string is the signal the
    upstream ``api_encryption`` tri-state already uses for "device
    confirmed plaintext" (issue #437) and the whole point of the
    debug collapsible is to make those advertisements observable.

    Order stability: zeroconf preserves the bytes-order of the
    raw TXT entries, which can shift on a fresh announce or when
    the cache rebuilds an entry. We sort by key so the wire
    output is deterministic across snapshots, letting downstream
    consumers dedupe with plain equality / ``JSON.stringify``
    instead of comparing dicts set-wise.

    The actual bytes-to-dict decode is delegated to
    ``_decode_txt_bytes_to_sorted_pairs`` so consecutive calls
    with the same TXT bytes reuse the cached result — typical
    for a stable fleet where each device's TXT rarely changes
    between firmware flashes.

    Returns ``{}`` when no TXT records are passed or the freshest
    record's ``text`` attribute is missing / not bytes-like.
    """
    if not txt_dns_records:
        return {}
    latest_txt = max(txt_dns_records, key=attrgetter("created"))
    txt_bytes = latest_txt.text
    if not isinstance(txt_bytes, (bytes, bytearray)):
        return {}
    return dict(_decode_txt_bytes_to_sorted_pairs(bytes(txt_bytes)))


def device_name_from_service(service_name: str) -> str:
    """Extract the device name from an mDNS service-instance name.

    The mDNS service announcement is
    ``<device-name>._esphomelib._tcp.local.``; the left-hand label is
    the device's ``esphome.name`` *verbatim* — modern configs use
    ``friendly_name_slugify``-style names with hyphens
    (``apollo-r-pro-1-eth-5938e0``) and the broadcast preserves them.
    Older underscored names (``my_device``) are likewise broadcast as
    given. Don't substitute hyphens for underscores or vice versa or
    the catalog lookup will silently miss every match.
    """
    return service_name.split(".", maxsplit=1)[0]


class DeviceStateMonitor:
    """
    Drive device state from mDNS broadcasts plus periodic ICMP pings.

    Only one source can own a device's state at a time. mDNS always
    wins; ping only writes when mDNS hasn't already resolved the
    device. The ``priority_for(name)`` API lets callers query which
    source is currently authoritative.
    """

    def __init__(
        self,
        get_devices: Callable[[], list[Device]],
        on_state_change: StateChangeCallback,
        on_ip_change: IPChangeCallback,
        on_version_change: VersionChangeCallback | None = None,
        on_config_hash_change: ConfigHashChangeCallback | None = None,
        on_api_encryption_change: ApiEncryptionChangeCallback | None = None,
        on_mac_address_change: MacAddressChangeCallback | None = None,
        on_importable_added: ImportableAddedCallback | None = None,
        on_importable_removed: ImportableRemovedCallback | None = None,
        reachability: ReachabilityTracker | None = None,
        is_ignored: Callable[[str], bool] | None = None,
        get_devices_by_name: Callable[[str], list[Device]] | None = None,
        presence: SubscriberPresence | None = None,
    ) -> None:
        self._get_devices = get_devices
        # ``get_devices_by_name`` is the O(1) name-keyed lookup that
        # the scanner exposes; mDNS / ping / MQTT observations key on
        # the device's ``esphome.name`` and call the apply-* methods
        # several times per broadcast, so a linear scan of every
        # configured YAML on every announcement is the obvious thing
        # not to do at fleet scale. Falls back to a linear scan when
        # the caller hasn't wired the index yet (kept so the existing
        # tests that build a monitor with just ``get_devices`` keep
        # working without a parallel rewrite).
        self._get_devices_by_name = get_devices_by_name or (
            lambda name: [d for d in get_devices() if d.name == name]
        )
        self._on_state_change = on_state_change
        self._on_ip_change = on_ip_change
        self._on_version_change = on_version_change
        self._on_config_hash_change = on_config_hash_change
        self._on_api_encryption_change = on_api_encryption_change
        self._on_mac_address_change = on_mac_address_change
        self._on_importable_added = on_importable_added
        self._on_importable_removed = on_importable_removed
        self._is_ignored = is_ignored or (lambda _name: False)
        self._state_source: dict[str, str] = {}  # device name → "mdns" | "ping"
        # Per-signal freshness tracker (mDNS / ping / MQTT last-seen,
        # ping RTT). Optional dependency: callers that don't care
        # about reachability metadata (the existing tests, in-process
        # usages that just want state-change forwarding) can pass
        # ``None`` and the monitor's observation hooks become no-ops.
        self._reachability = reachability
        # ``DashboardImportDiscovery`` is the upstream esphome class
        # that watches the same ``_esphomelib._tcp.local.`` browser for
        # ``package_import_url`` TXT records and turns them into
        # ``DiscoveredImport`` entries. Hooking it as a sibling
        # browser-callback keeps us in lockstep with whatever the
        # upstream considers an importable device.
        self._import_discovery: DashboardImportDiscovery | None = None
        # Map of device-name → web-UI URL, populated by the
        # ``_http._tcp.local.`` browser. Lets the discovered-device
        # card render a Visit-web-UI link without the frontend having
        # to know which factory firmwares ship a web server.
        self._http_urls: dict[str, str] = {}
        self._zeroconf: AsyncEsphomeZeroconf | None = None
        # Single browser covers both ``_esphomelib._tcp.local.`` and
        # ``_http._tcp.local.``; the dispatch handler routes events
        # by ``service_type`` to the right per-type logic.
        self._mdns_browser: AsyncServiceBrowser | None = None
        self._ping_task: asyncio.Task | None = None
        # Strong refs for fire-and-forget mDNS resolve tasks so the
        # garbage collector can't reap them mid-await.
        self._tasks: set[asyncio.Task] = set()
        # DNS resolutions for non-mDNS hostnames are cached here so the
        # ping sweep, OTA cache args, and device.ip tracking all share
        # the same TTL'd lookup result instead of re-resolving every
        # cycle.
        self._dns_cache = DNSCache()
        # When wired, the ping loop pauses while no dashboard client
        # is subscribed — so a quiet network with no observers
        # generates no ICMP traffic. Mirrors the legacy
        # esphome.dashboard.status.ping behaviour (``while
        # self._subscribers`` in web_server.py + ``await
        # dashboard.ping_request.wait()`` in ping.py); reaching
        # parity here closes a regression in the new dashboard,
        # which had been ping-sweeping unconditionally. Optional so
        # existing tests that build a monitor without a presence
        # gate keep working; ``None`` means "always run the loop".
        self._presence = presence

    async def start(self) -> None:
        """Start the mDNS browser and the periodic ping sweep."""
        await self._start_mdns_browser()
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def stop(self) -> None:
        """Tear down the browser and cancel the ping loop."""
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        # Cancel the browser FIRST so it stops dispatching new mDNS
        # callbacks. If we drained ``self._tasks`` first, the browser
        # could still spawn new resolve tasks during the ``gather``
        # await and they'd miss the snapshot we took.
        if self._mdns_browser is not None:
            try:
                await self._mdns_browser.async_cancel()
            except Exception:
                _LOGGER.debug("mDNS browser cancel failed", exc_info=True)
            self._mdns_browser = None
        # Now drain any in-flight resolve tasks. New tasks can no
        # longer appear, so a single snapshot is safe.
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        if self._zeroconf is not None:
            try:
                await self._zeroconf.async_close()
            except Exception:
                _LOGGER.debug("zeroconf close failed", exc_info=True)
            self._zeroconf = None

    @property
    def zeroconf(self) -> AsyncEsphomeZeroconf | None:
        """
        The mDNS responder powering device discovery, or ``None``.

        Exposed so the dashboard's own ``_esphomebuilder._tcp.local.``
        advertiser can reuse the same instance instead of opening a
        second responder. Returns ``None`` when zeroconf failed to
        start (port held by avahi / ``mDNSResponder``); callers are
        expected to skip their advertise in that case.
        """
        return self._zeroconf

    def set_reachability(self, tracker: ReachabilityTracker) -> None:
        """Wire (or rewire) the per-signal freshness tracker.

        ``DevicesController`` builds the state monitor first so the
        tracker can take ``get_mdns_cache_info`` as its mDNS cache
        reader; this setter completes the wire-back so the
        monitor's ``apply`` path can route observations into the
        tracker.
        """
        self._reachability = tracker

    def priority_for(self, name: str) -> ReachabilitySource:
        """Return the source currently authoritative for *name*.

        Returns :data:`ReachabilitySource.UNKNOWN` when no source has
        claimed the device. Callers comparing against literal source
        strings keep working because :class:`ReachabilitySource` is a
        :class:`StrEnum` and equality with the underlying ``str``
        passes through. Made enum-typed so the drawer's reachability
        subscription can dispatch on it without a string-typo
        landing as silent UNKNOWN.
        """
        return ReachabilitySource(self._state_source.get(name, ReachabilitySource.UNKNOWN))

    def apply(self, name: str, state: DeviceState, source: str, *, claim: bool = False) -> bool:
        """
        Record a state observation from *source*.

        Returns True when the observation actually changed at least
        one matching device's state and the change was forwarded to
        the callback. Sources below the current source's priority
        are ignored; observations where every matching device
        already carries *state* are no-ops.

        ``claim=True`` lets *source* take ownership of the device's
        state slot even when the state is unchanged, so that a
        higher-priority observation arriving after a lower-priority
        one already pinned the same state can still prevent the
        lower-priority source from later flipping it back. The
        priority check still applies — ``claim`` doesn't let a lower-
        priority source override a higher-priority owner.
        """
        devices = self._get_devices_by_name(name)
        if not devices:
            _LOGGER.debug(
                "Device %s not in catalog — ignoring %s state from %s", name, state, source
            )
            return False

        # Record the per-signal observation regardless of whether the
        # priority check below ends up ignoring the new state. The user-
        # facing intent is "show every channel we're hearing on,
        # independently" — a higher-priority source claiming the device
        # shouldn't hide that ping or MQTT also just answered. The
        # ONLINE filter avoids treating "lost" signals (the OFFLINE
        # flips ping / mqtt issue when the source itself drops) as
        # freshness.
        if state == DeviceState.ONLINE and self._reachability is not None:
            self._reachability.observe(name, source)

        current_source = self._state_source.get(name, ReachabilitySource.UNKNOWN)
        if _SOURCE_PRIORITY.get(source, 0) < _SOURCE_PRIORITY.get(current_source, 0):
            return False
        # Dedupe must look at *every* matching device, not just the
        # first. With duplicate ``esphome.name`` entries (a config
        # plus a ``foo (1).yaml`` copy, dashboard_import siblings)
        # one sibling can be in-sync while another was rebuilt with
        # state=UNKNOWN — the old "first device matches → bail" path
        # left the stale sibling stuck.
        if all(d.state == state for d in devices):
            if claim:
                self._state_source[name] = source
            return False

        self._state_source[name] = source
        self._on_state_change(name, state, source)
        return True

    async def refresh_mdns(self, name: str) -> None:
        """Re-query a device's mDNS A/AAAA records via the wire.

        Caller (the drawer's reachability subscription) is
        expected to schedule this *after* the cached A record's
        TTL has elapsed — at that point ``async_resolve_host``'s
        ``load_from_cache`` short-circuit fails (the record is
        expired and skipped by ``_process_record_threadsafe``),
        the call falls through to ``async_request``, and we
        actually go on the wire.

        ESPHome devices are mDNS-silent except in response to
        probes, so this is the only mechanism that keeps an
        A record alive once it ages out. The
        ``ServiceBrowser``-managed PTR has a 4500s TTL and is
        kept alive by the browser, but A's 120s TTL decays on
        its own and the browser does not re-query A.

        No-op when zeroconf failed to start.
        """
        if self._zeroconf is None:
            return
        try:
            addresses = await self._zeroconf.async_resolve_host(
                f"{name}.local", _MDNS_HOSTNAME_RESOLVE_TIMEOUT
            )
        except Exception:
            _LOGGER.debug("mDNS refresh of %s failed", name, exc_info=True)
            return
        self._apply_resolved_addresses(name, addresses)

    def _get_address_records(self, name: str) -> list[Any]:
        """Return cached A and AAAA records for *name*, or ``[]``.

        Used by both :meth:`get_mdns_a_record_ttl_remaining`
        (which scopes to address records to drive the refresh
        loop) and :meth:`get_mdns_cache_info` (which folds the
        addresses into a union with SRV / TXT / PTR for the
        drawer's "last seen" display).
        """
        if self._zeroconf is None:
            return []
        cache = self._zeroconf.zeroconf.cache
        local_name = f"{name}.local."
        return [
            *cache.get_all_by_details(local_name, _TYPE_A, _CLASS_IN),
            *cache.get_all_by_details(local_name, _TYPE_AAAA, _CLASS_IN),
        ]

    def get_mdns_a_record_ttl_remaining(self, name: str) -> float | None:
        """Return the minimum remaining TTL across cached A/AAAA records.

        Distinct from :meth:`get_mdns_cache_info` because the
        drawer's refresh loop needs the A-record-specific
        expiry to schedule its next wire query — not the
        union-of-types "last seen" age the snapshot uses for
        display. PTR has a 4500s TTL and stays cached for
        ages, so a sleep based on the PTR's remaining TTL
        would never trigger the A-record refresh that's the
        whole point of the loop.

        Returns the smallest remaining TTL across whatever
        A/AAAA records are cached (covers the case where one
        family expires before the other), or ``None`` if no
        A/AAAA is cached.
        """
        records = self._get_address_records(name)
        if not records:
            return None
        now_ms = current_time_millis()
        return max(0.0, min(float(r.get_remaining_ttl(now_ms)) for r in records))

    def get_mdns_cache_info(self, name: str) -> MdnsCacheInfo | None:
        """
        Read the truthful "last heard via mDNS" age + remaining TTL.

        Returns the most-recent ``DNSRecord.created`` across
        every cached record we have for the device, paired with
        the matching record's
        :meth:`zeroconf.DNSRecord.get_remaining_ttl`. The records
        we look at:

        * ``A`` / ``AAAA`` at ``<name>.local.`` — the IP-address
          announces (120s TTL by default).
        * ``SRV`` / ``TXT`` at ``<name>._esphomelib._tcp.local.``
          — the API service-instance records (only present for
          devices running the native API).
        * ``PTR`` at ``_esphomelib._tcp.local.`` filtered to
          alias matches — the long-TTL pointer record
          (~4500s) the ``ServiceBrowser`` keeps alive.

        Walking multiple record types matters because each one
        has its own TTL: A/AAAA decay at 120s, but the PTR
        kept alive by the browser stays fresh for tens of
        minutes. After A expires, an SRV refresh from a probe
        — or even just the still-live PTR — still tells us
        "we heard mDNS for this device N seconds ago"
        truthfully, which is what the drawer's "Last seen"
        line is asking. Only when *every* record we know about
        has been evicted from the cache do we return ``None``
        (and the drawer hides the mDNS row).

        Returns ``None`` when zeroconf isn't running, or when
        the cache has nothing under any of the record types we
        check.
        """
        if self._zeroconf is None:
            return None
        cache = self._zeroconf.zeroconf.cache
        service_name = f"{name}.{_ESPHOME_SERVICE_TYPE}"
        txt_dns_records = list(cache.get_all_by_details(service_name, _TYPE_TXT, _CLASS_IN))
        records: list[Any] = [
            *self._get_address_records(name),
            *cache.get_all_by_details(service_name, _TYPE_SRV, _CLASS_IN),
            *txt_dns_records,
        ]
        # PTR is owned by the type-domain (``_esphomelib._tcp.local.``)
        # and carries the service-instance as its ``alias`` —
        # zeroconf already exposes ``current_entry_with_name_and_alias``
        # for exactly this lookup so we don't have to walk every
        # PTR and filter ourselves. Helper filters expired
        # internally, which is fine for the 4500s-TTL PTR (won't
        # expire in any realistic drawer-open window).
        ptr = cache.current_entry_with_name_and_alias(_ESPHOME_SERVICE_TYPE, service_name)
        if ptr is not None:
            records.append(ptr)
        if not records:
            return None
        # Don't filter expired records — the drawer wants the
        # truthful "last seen" age even when the cached record
        # has aged past its TTL. With multiple record types
        # contributing, the PTR (~4500s TTL) typically
        # outlives A/AAAA (120s) so the row stays populated
        # via the PTR's ``created`` even during the brief
        # expiry-to-refresh window for the address records.
        # The row only hides once *every* cached record has
        # been evicted, which the empty-check above handles.
        now_ms = current_time_millis()
        latest = max(records, key=attrgetter("created"))
        # ``DNSAddress.created`` is millis; ``now_ms - created`` is
        # millis, hence ``millis_to_seconds`` here.
        age_s = max(0.0, millis_to_seconds(now_ms - latest.created))
        # ``get_remaining_ttl`` already returns seconds (the
        # impl divides by 1000.0 internally). Don't convert again
        # — that would turn "108 seconds remaining" into 0.108
        # and render as "TTL: 0s".
        ttl_remaining_s = max(0.0, float(latest.get_remaining_ttl(now_ms)))
        return MdnsCacheInfo(
            age_seconds=age_s,
            ttl_remaining_seconds=ttl_remaining_s,
            txt_records=_decode_mdns_txt_records(txt_dns_records),
        )

    def _apply_resolved_addresses(
        self, name: str, addresses: list[str] | BaseException | None
    ) -> None:
        """Funnel a successful active-resolve into the apply path.

        Both the per-subscription :meth:`refresh_mdns` and the
        batch :meth:`_resolve_non_api_mdns_targets` need the same
        "non-empty address list → claim mDNS-ONLINE + record IPs"
        treatment. Sharing the branch keeps the deliberate
        no-OFFLINE-on-miss rule (documented at the call site in
        :meth:`_resolve_non_api_mdns_targets`) consistent across
        both paths.

        ``addresses`` accepts the union ``asyncio.gather(...,
        return_exceptions=True)`` produces so the batch path can
        thread its results in without a per-element type check.
        """
        if isinstance(addresses, list) and addresses:
            self.apply(name, DeviceState.ONLINE, "mdns", claim=True)
            self.apply_ip_addresses(name, addresses)

    def apply_ip(self, name: str, ip: str) -> bool:
        """
        Record a single-IP observation. Empty string clears the stored IPs.

        Returns True when the IP actually changed and the change was
        forwarded to the callback. Used by sources that only know one
        address per device (MQTT discovery, DNS resolve fallback);
        callers with the full announced set should reach for
        :meth:`apply_ip_addresses` instead so the multi-IP view stays
        accurate.

        When *ip* is already present in the device's ``ip_addresses``
        list, only the primary slot is touched — a narrower MQTT /
        DNS observation must not shrink a multi-IP view that mDNS
        already populated, otherwise we'd re-hide IPv6 the next time
        MQTT discovery fires.
        """
        if not ip:
            return self._dispatch_ip(name, "", [])
        devices = self._get_devices_by_name(name)
        if not devices:
            return False
        # Read ``ip_addresses`` off any matching device — duplicates
        # all flow through ``_dispatch_ip``'s fan-out so they end up
        # at the same state regardless of which we sampled here.
        existing = devices[0].ip_addresses
        addresses = list(existing) if ip in existing else [ip]
        return self._dispatch_ip(name, ip, addresses)

    def apply_ip_addresses(self, name: str, addresses: list[str]) -> bool:
        """
        Record the full set of announced IPs for *name*.

        Picks an IPv4 primary via :func:`_pick_ipv4` (falling back to
        the first scoped IPv6 when no V4 is present) so ``device.ip``
        keeps its "single IP we'll hand to ICMP / OTA" shape, and
        forwards the complete list so ``device.ip_addresses`` reflects
        what the device is broadcasting. Empty list clears both.
        """
        primary = _pick_ipv4(addresses) if addresses else ""
        return self._dispatch_ip(name, primary, addresses)

    def _dispatch_ip(self, name: str, primary: str, addresses: list[str]) -> bool:
        """
        Shared dedupe + dispatch for both apply_ip variants.

        Dedupe is done against the configured devices' current ``ip``
        and ``ip_addresses`` fields rather than a separate monitor
        cache so a Device that's been rebuilt with ``previous=None``
        (e.g. an atomic save's brief REMOVE+re-ADD scan churn) still
        gets repopulated by the next mDNS announcement. Either side
        differing is enough to fire — a host that picks up an IPv6
        address while keeping the same IPv4 still surfaces in the
        dashboard.
        """
        devices = self._get_devices_by_name(name)
        if not devices:
            return False
        if all(d.ip == primary and d.ip_addresses == addresses for d in devices):
            return False
        self._on_ip_change(name, primary, addresses)
        return True

    def apply_version(self, name: str, version: str) -> bool:
        """
        Record a firmware version observation.

        Returns True when the version actually changed and the change
        was forwarded to the callback.
        """
        if not version or self._on_version_change is None:
            return False
        if not self._any_matching_device_differs(name, "deployed_version", version):
            return False
        self._on_version_change(name, version)
        return True

    def apply_api_encryption(self, name: str, encryption: str) -> bool:
        """
        Record the device's broadcast API encryption status.

        Non-empty value (e.g. ``Noise_NNpsk0_25519_ChaChaPoly_SHA256``)
        confirms encryption is active. Empty string is the
        plaintext-confirmed signal, fired by ``_apply_service_info``
        in two wire shapes:

        1. TXT carries the ``api_encryption`` key with an empty /
           bare value (``api_encryption=`` or just ``api_encryption``
           — zeroconf collapses both to ``None`` and the apply path
           normalises to ``""``).

        2. TXT carries other content (``version`` / ``mac`` /
           ``config_hash`` / ...) but the ``api_encryption`` key is
           absent. ESPHome firmware emits TXT atomically per
           announce, so the omission of the key inside an
           otherwise-populated TXT IS authoritative for "encryption
           was removed."

        Callers must NOT translate "no TXT content at all" into
        ``""`` — that conflates a transient cache-eviction /
        truly-empty fragment with a real plaintext confirmation,
        which would clobber the last-known truthy value and trip
        the frontend's "reinstall to apply" prompt.
        ``_apply_service_info`` only fires the empty-string apply
        when the announce carried other content; the truly-empty
        case skips the call entirely so the device's
        ``api_encryption_active`` stays ``None`` (or whatever was
        last confirmed) until a real observation lands.

        Returns True when the value actually changed and the change
        was forwarded to the callback.
        """
        if self._on_api_encryption_change is None:
            return False
        if not self._any_matching_device_differs(name, "api_encryption_active", encryption):
            return False
        self._on_api_encryption_change(name, encryption)
        return True

    def apply_config_hash(self, name: str, config_hash: str) -> bool:
        """
        Record a running-firmware config hash observation.

        Returns True when the hash actually changed and the change was
        forwarded to the callback. Empty strings are dropped so devices
        running pre-#16145 firmware (no ``config_hash`` TXT) don't churn
        the callback.
        """
        if not config_hash or self._on_config_hash_change is None:
            return False
        if not self._any_matching_device_differs(name, "deployed_config_hash", config_hash):
            return False
        self._on_config_hash_change(name, config_hash)
        return True

    def apply_mac_address(self, name: str, mac: str) -> bool:
        """
        Record a MAC-address observation from the device's mDNS TXT.

        Returns True when the MAC actually changed and the change was
        forwarded to the callback. The broadcast value is normalized
        via :func:`_normalize_mac` (uppercased, separators stripped,
        re-inserted as ``XX:XX:XX:XX:XX:XX``) so the dedupe +
        persisted sidecar + frontend wire all stay canonical even if
        a future firmware switches case or separator style. Empty /
        non-hex inputs are dropped so a broadcast that happens to
        omit the ``mac`` TXT (older firmware, non-ESPHome services
        that share the type) doesn't blank out an already-known MAC.
        """
        if self._on_mac_address_change is None:
            return False
        normalized = _normalize_mac(mac)
        if not normalized:
            return False
        if not self._any_matching_device_differs(name, "mac_address", normalized):
            return False
        self._on_mac_address_change(name, normalized)
        return True

    def _any_matching_device_differs(self, name: str, attr: str, value: Any) -> bool:
        """Return True iff some configured device named *name* has ``attr != value``.

        Uses the scanner's ``get_devices_by_name`` index for an O(1)
        name lookup so a 1000-device fleet doesn't pay an O(N) scan
        on every mDNS broadcast. Short-circuits the moment a stale
        match is found; returns False when no device matches *name*
        (stray announcement) or when every match already carries
        *value* (steady-state dedupe).
        """
        return any(getattr(device, attr) != value for device in self._get_devices_by_name(name))

    def get_cached_addresses(self, host_name: str) -> list[str] | None:
        """
        Return all zeroconf-cached IPs for *host_name* without issuing a query.

        Both IPv4 and IPv6 (scoped) entries are included — the OTA
        address-cache CLI args need every IP we know so the runtime
        can try them in turn. Callers that want a single best target
        for, say, ICMP should pick IPv4 first themselves.

        Returns ``None`` when zeroconf isn't running, the cache misses,
        or the entry has expired. mDNS-only — see
        :meth:`get_cached_dns_addresses` for non-``.local`` hostnames.
        """
        if self._zeroconf is None:
            return None

        normalized = normalize_hostname(host_name)
        base_name = normalized.partition(".")[0]
        resolver_name = f"{base_name}.local."
        info = AddressResolver(resolver_name)
        if not info.load_from_cache(self._zeroconf.zeroconf):
            return None
        addresses = info.parsed_scoped_addresses(IPVersion.All)
        return addresses or None

    def probe_device(self, device_name: str, service_name: str | None = None) -> None:
        """Eagerly resolve a device's ``_esphomelib._tcp.local.`` service.

        Adoption / import / wizard-created devices land in the
        configured catalog the moment we write their YAML, but the
        regular browser path only updates ONLINE / IP / version /
        config_hash / api_encryption when the *next* mDNS announcement
        arrives — which can be minutes for a quiet device. This method
        short-circuits the wait by either reading the existing
        zeroconf cache (sync hit, common case for a device that was
        just on the discovery banner) or kicking off an
        ``async_request`` in a fire-and-forget task. Either way the
        apply path is the same one the browser uses, so the device's
        card flips from "Unknown" to a fully-populated card
        immediately instead of on the next periodic sweep.

        ``service_name`` defaults to ``device_name`` and is the
        broadcast name to look up in mDNS. Adoption surfaces a
        device whose mDNS-advertised name (the original factory
        firmware's hostname) differs from the user-chosen YAML name;
        passing it explicitly lets the lookup hit the cached service
        info while the apply still keys to the configured device's
        name.
        """
        if self._zeroconf is None:
            return
        zeroconf = self._zeroconf.zeroconf
        broadcast = service_name or device_name
        full_service = f"{broadcast}.{_ESPHOME_SERVICE_TYPE}"
        info = AsyncServiceInfo(_ESPHOME_SERVICE_TYPE, full_service)
        if info.load_from_cache(zeroconf):
            self._apply_service_info(device_name, info)
            return
        task = asyncio.create_task(self._resolve_and_apply(zeroconf, info, device_name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def revisit_importable(self, device_name: str) -> None:
        """
        Re-fire ``on_importable_added`` for *device_name* if upstream still has it cached.

        Used after a configured device is deleted: the device's mDNS
        announcement was being suppressed by the ``configured-name``
        filter in ``_on_import_update``, but upstream's
        ``DashboardImportDiscovery.import_state`` already has the
        ``DiscoveredImport`` entry from the original announcement.
        Without this nudge the discovery banner stays silent until the
        device re-announces (which can be minutes for a quiet device).

        Ignored devices are skipped — the user already said "don't
        show me this", so a deletion shouldn't unilaterally bring it
        back. They can unignore through the menu if they change their
        mind, and an unsolicited mDNS re-announce will surface it
        through the normal callback path either way.
        """
        if self._import_discovery is None or self._is_ignored(device_name):
            return
        for service_name, discovered in self._import_discovery.import_state.items():
            if discovered.device_name == device_name:
                self._on_import_update(service_name, discovered)

    def revisit_all_importables(self) -> None:
        """
        Re-fire ``on_importable_added`` for every cached importable.

        Used when a configured YAML is deleted but we don't know what
        mDNS name it came from (the user may have picked a YAML name
        that differs from the discovered hostname during adoption).
        ``_on_import_update`` already filters configured + ignored
        names so re-emitting the full set is safe; only the entries
        that should appear in the banner do.
        """
        if self._import_discovery is None:
            return
        for service_name, discovered in self._import_discovery.import_state.items():
            self._on_import_update(service_name, discovered)

    def get_importable_devices(self) -> list[AdoptableDevice]:
        """
        Snapshot of devices currently advertising as importable.

        Built fresh each call from ``DashboardImportDiscovery``'s
        ``import_state`` so the ``ignored`` flag and the configured-
        device filter both reflect the live dashboard state. Callers
        (e.g. the WebSocket ``initial_state`` event) get the same view
        the per-device ADDED events would have surfaced incrementally.
        """
        if self._import_discovery is None:
            return []
        configured_names = {d.name for d in self._get_devices()}
        out: list[AdoptableDevice] = []
        for discovered in self._import_discovery.import_state.values():
            if discovered.device_name in configured_names:
                continue
            out.append(self._build_adoptable(discovered))
        return out

    def get_cached_dns_addresses(self, host_name: str) -> list[str] | None:
        """
        Return DNS-cached IPs for *host_name* without issuing a lookup.

        Populated by the ping sweep's pre-resolution pass. Returns
        ``None`` on cache miss or when the entry has expired.
        """
        return self._dns_cache.get_cached_addresses(host_name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_device_by_name(self, name: str) -> Device | None:
        bucket = self._get_devices_by_name(name)
        return bucket[0] if bucket else None

    async def _start_mdns_browser(self) -> None:
        try:
            self._zeroconf = AsyncEsphomeZeroconf()
        except Exception:
            _LOGGER.exception("Could not start zeroconf — falling back to ping only")
            self._zeroconf = None
            return

        def _on_esphomelib_service_state_change(
            zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
        ) -> None:
            # ``AsyncServiceBrowser`` dispatches handlers on the asyncio
            # loop, so call apply methods directly. For Added/Updated,
            # try the zeroconf cache first (sync) — only fall back to a
            # network query (async task) when the cache misses.
            device_name = device_name_from_service(name)
            _LOGGER.debug("mDNS: %s %s (raw: %s)", state_change, device_name, name)

            # Short-circuit unconfigured devices so we don't spawn
            # ServiceInfo lookups / resolve tasks for unrelated ESPHome
            # nodes on the LAN.
            if self._find_device_by_name(device_name) is None:
                return

            if state_change == ServiceStateChange.Removed:
                self.apply(device_name, DeviceState.OFFLINE, "mdns")
                self.apply_ip(device_name, "")
                self._state_source.pop(device_name, None)
                if self._reachability is not None:
                    self._reachability.clear(device_name)
                return

            # ``claim=True`` so mDNS takes ownership even when the
            # device is already ONLINE via a lower-priority source
            # (ping / MQTT), preventing later ping observations from
            # clobbering the now-authoritative mDNS view.
            self.apply(device_name, DeviceState.ONLINE, "mdns", claim=True)

            info = AsyncServiceInfo(service_type, name)
            if info.load_from_cache(zeroconf):
                self._apply_service_info(device_name, info)
                return

            task = asyncio.create_task(self._resolve_and_apply(zeroconf, info, device_name))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        # ``DashboardImportDiscovery`` from upstream esphome owns the
        # TXT-record parsing for adoptable factory firmwares — its
        # ``browser_callback`` only acts on services that carry the
        # ``package_import_url`` TXT records, so harmlessly receiving
        # HTTP events is fine.
        self._import_discovery = DashboardImportDiscovery(self._on_import_update)

        def _dispatch(
            zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
        ) -> None:
            # Single ``AsyncServiceBrowser`` covers both service types;
            # dispatch by ``service_type`` so each inner handler only
            # sees the events it cares about. Sharing one browser
            # halves the zeroconf bookkeeping vs running two separate
            # browsers and lets the upstream ``DashboardImportDiscovery``
            # callback piggy-back on the same dispatch path.
            if service_type == _ESPHOME_SERVICE_TYPE:
                _on_esphomelib_service_state_change(zeroconf, service_type, name, state_change)
                self._import_discovery.browser_callback(zeroconf, service_type, name, state_change)
            elif service_type == _HTTP_SERVICE_TYPE:
                self._on_http_service_state_change(zeroconf, service_type, name, state_change)

        try:
            self._mdns_browser = AsyncServiceBrowser(
                self._zeroconf.zeroconf,
                [_ESPHOME_SERVICE_TYPE, _HTTP_SERVICE_TYPE],
                handlers=[_dispatch],
            )
            _LOGGER.info(
                "mDNS browser started for %s, %s",
                _ESPHOME_SERVICE_TYPE,
                _HTTP_SERVICE_TYPE,
            )
        except Exception:
            _LOGGER.exception("Could not start mDNS browser — device discovery limited to ping")

    async def _resolve_and_apply(
        self, zeroconf: Any, info: AsyncServiceInfo, device_name: str
    ) -> None:
        """Resolve a cache-miss esphomelib mDNS service and propagate its details."""
        await self._resolve_then(zeroconf, info, device_name, self._apply_service_info)

    async def _resolve_then(
        self,
        zeroconf: Any,
        info: AsyncServiceInfo,
        device_name: str,
        apply: Callable[[str, AsyncServiceInfo], None],
    ) -> None:
        """Resolve a cache-miss service and hand the result to *apply*.

        The esphomelib and HTTP browsers share the same fire-and-forget
        shape: spawn a task on cache miss, ``async_request`` the
        record, swallow exceptions to a debug log, then dispatch to
        the per-type applier when resolution succeeds.
        """
        try:
            if not await info.async_request(zeroconf, timeout=_MDNS_RESOLVE_TIMEOUT_MS):
                return
        except Exception:
            _LOGGER.debug("mDNS resolve failed for %s", device_name, exc_info=True)
            return
        apply(device_name, info)

    def _on_http_service_state_change(
        self,
        zeroconf: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Track ``_http._tcp.local.`` services so discovered cards can show a Visit-web-UI link.

        The browser fires for every HTTP service on the LAN — we only
        care about the ones whose left-hand label matches an importable
        device, so the matching is name-driven. When an HTTP service
        appears (or disappears) for an existing importable, re-emit
        the entry so the card's ``web_url`` field stays in sync
        without waiting for the next esphomelib announcement.
        """
        device_name = device_name_from_service(name)
        if state_change == ServiceStateChange.Removed:
            if self._http_urls.pop(device_name, None) is None:
                return
            self._refire_importable_for(device_name)
            return

        info = AsyncServiceInfo(service_type, name)
        if info.load_from_cache(zeroconf):
            self._apply_http_service_info(device_name, info)
            return
        task = asyncio.create_task(self._resolve_and_apply_http(zeroconf, info, device_name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve_and_apply_http(
        self, zeroconf: Any, info: AsyncServiceInfo, device_name: str
    ) -> None:
        """Resolve a cache-miss HTTP service and store its URL."""
        await self._resolve_then(zeroconf, info, device_name, self._apply_http_service_info)

    def _apply_http_service_info(self, device_name: str, info: AsyncServiceInfo) -> None:
        """Build the Visit-web-UI URL from a populated HTTP service info.

        Only stored when an importable device with the same name is
        currently advertising. Without this guard ``_http_urls`` grew
        unbounded from every HTTP service on the LAN (printers, NAS
        boxes, routers — none of which we have any use for); this
        keeps the cache scoped to entries that can actually drive a
        Visit-web-UI link on the discovered card.
        """
        if not self._has_importable(device_name):
            return
        url = _http_url_from_service_info(device_name, info)
        if self._http_urls.get(device_name) == url:
            return
        self._http_urls[device_name] = url
        self._refire_importable_for(device_name)

    def _has_importable(self, device_name: str) -> bool:
        """Return True when an importable currently exists for *device_name*."""
        if self._import_discovery is None:
            return False
        return any(
            d.device_name == device_name for d in self._import_discovery.import_state.values()
        )

    def _refire_importable_for(self, device_name: str) -> None:
        """Re-emit ADDED for *device_name* so frontends pick up a web_url change."""
        if self._import_discovery is None:
            return
        for service_name, discovered in self._import_discovery.import_state.items():
            if discovered.device_name == device_name:
                self._on_import_update(service_name, discovered)
                return

    def _seed_http_url_from_cache(self, device_name: str) -> None:
        """Pull ``device_name``'s HTTP service URL out of zeroconf's cache.

        Handles the case where the HTTP service arrived first: the
        browser callback skipped storing the URL because no importable
        existed for that name yet. Now that one does, look directly at
        zeroconf's cache (no network round-trip) and stash the URL so
        the about-to-fire ``on_importable_added`` carries the right
        ``web_url``.
        """
        if self._zeroconf is None or self._http_urls.get(device_name):
            return
        info = AsyncServiceInfo(_HTTP_SERVICE_TYPE, f"{device_name}.{_HTTP_SERVICE_TYPE}")
        if not info.load_from_cache(self._zeroconf.zeroconf):
            return
        self._http_urls[device_name] = _http_url_from_service_info(device_name, info)

    def _on_import_update(self, service_name: str, discovered: DiscoveredImport | None) -> None:
        """Bridge ``DashboardImportDiscovery`` → controller callbacks.

        ``service_name`` is the full mDNS service-instance name
        (``<device>._esphomelib._tcp.local.``); ``discovered`` is None
        on removal. We re-key by device name so callers don't have to
        carry the suffix, drop devices that are already configured
        locally (since the dashboard knows about them already), and
        translate the upstream ``DiscoveredImport`` shape into our
        ``AdoptableDevice`` model with the ``ignored`` flag filled in.
        """
        device_name = device_name_from_service(service_name)
        if discovered is None:
            if self._on_importable_removed is not None:
                self._on_importable_removed(device_name)
            return
        if self._find_device_by_name(device_name) is not None:
            # Already configured — surfacing it as importable would
            # confuse the dashboard.
            return
        # Late-binding: if the HTTP service for this device is already
        # in zeroconf's cache (it arrived before the esphomelib
        # service), pull its URL now so the AdoptableDevice we emit
        # here carries it without waiting for the next HTTP re-announce.
        self._seed_http_url_from_cache(discovered.device_name)
        if self._on_importable_added is not None:
            self._on_importable_added(self._build_adoptable(discovered))

    def _build_adoptable(self, discovered: DiscoveredImport) -> AdoptableDevice:
        """Translate an upstream ``DiscoveredImport`` into our ``AdoptableDevice``.

        Single construction site for the cross-type mapping plus the
        two locally-known fields (``ignored`` from the persisted set,
        ``web_url`` from the HTTP-service cache). Used by both the
        live ADD path (``_on_import_update``) and the snapshot path
        (``get_importable_devices``) so the two views stay identical.
        """
        return AdoptableDevice(
            name=discovered.device_name,
            friendly_name=discovered.friendly_name or "",
            package_import_url=discovered.package_import_url,
            project_name=discovered.project_name,
            project_version=discovered.project_version,
            network=discovered.network,
            ignored=self._is_ignored(discovered.device_name),
            web_url=self._http_urls.get(discovered.device_name, ""),
        )

    def _apply_service_info(self, device_name: str, info: AsyncServiceInfo) -> None:
        """Pull IP / version / config_hash off a populated ``AsyncServiceInfo``.

        A successful apply is itself proof the device is reachable —
        we have its broadcast TXT records and address from zeroconf —
        so claim ONLINE under the mDNS source. The browser callback
        already calls ``apply(...ONLINE..., claim=True)`` itself, so
        for that path this is a no-op dedupe; the eager
        ``probe_device`` path needs it because it skips the
        browser-callback prelude.
        """
        # ``claim=True`` so mDNS owns the slot even when ping/MQTT
        # had already labelled the device — same shape the browser
        # callback uses on its way into this method.
        self.apply(device_name, DeviceState.ONLINE, "mdns", claim=True)
        # Pull every announced address (IPv4 first, then scoped IPv6
        # — link-local entries keep the ``%scope`` suffix that's
        # required to connect at all). ``apply_ip_addresses`` picks
        # the IPv4 primary for ``device.ip`` and forwards the whole
        # list so ``device.ip_addresses`` reflects what's actually
        # broadcast — a multi-homed dual-stack device used to surface
        # only its V4 here.
        if addresses := info.parsed_scoped_addresses(IPVersion.All):
            self.apply_ip_addresses(device_name, addresses)
        # ``decoded_properties`` is a ``dict[str, str | None]`` — zeroconf
        # already handles the UTF-8 decode and None-on-bad-bytes for us.
        props = info.decoded_properties
        if version := props.get("version"):
            self.apply_version(device_name, version)
        if config_hash := props.get("config_hash"):
            self.apply_config_hash(device_name, config_hash)
        if mac := props.get("mac"):
            self.apply_mac_address(device_name, mac)
        # api_encryption tri-state semantics on this announce:
        #
        # * Key present with truthy value (``Noise_...``):
        #   encryption confirmed live → apply with that string.
        #
        # * Key present with bare-key / ``api_encryption=`` empty
        #   value (zeroconf collapses both to ``None`` in
        #   ``decoded_properties``): device explicitly broadcast
        #   "no key" → apply with ``""``. The pre-fix code used
        #   ``props.get("api_encryption") is not None`` which
        #   dropped this case onto the floor; with the explicit
        #   ``in props`` check it now flows through.
        #
        # * Key absent AND the announce carried other content
        #   (``version`` / ``mac`` / ``config_hash`` / ...): the
        #   firmware was rebuilt without encryption and is
        #   re-announcing its real new state. Apply with ``""``
        #   so the dashboard's encryption indicator follows the
        #   wire instead of staying frozen on a stale truthy
        #   value. ESPHome's TXT broadcasts are atomic per
        #   announce — there's no fragmentation shape that would
        #   carry ``version`` but drop ``api_encryption`` — so a
        #   TXT with content but no encryption key IS
        #   authoritative for "encryption was removed."
        #
        # * Key absent AND props is empty (no other keys either):
        #   preserve. This is the cache-eviction /
        #   truly-empty-fragment shape the original guard was
        #   written for; a non-content announce shouldn't
        #   overwrite a previously-truthy state and prompt an
        #   unnecessary reinstall.
        #
        # Older firmwares that never broadcast the TXT keep the
        # ``None`` initial value — the frontend's
        # ``getEncryptionState`` falls back to the YAML's
        # ``api_encrypted`` flag in that case, which is the right
        # behaviour.
        if "api_encryption" in props:
            value = props["api_encryption"]
            self.apply_api_encryption(device_name, value if isinstance(value, str) else "")
        elif props:
            self.apply_api_encryption(device_name, "")

    async def _ping_loop(self) -> None:
        # First sweep after the short bootstrap window — gives mDNS a
        # head start so we don't redundantly ping devices the browser
        # is about to flip ONLINE for free, but still gets the UNKNOWN
        # → OFFLINE transition in front of the user within ~10s of
        # startup instead of after a full minute.
        await asyncio.sleep(_PING_BOOTSTRAP_DELAY)
        # Strict pause: when wired to a SubscriberPresence gate, only
        # sweep while at least one dashboard client is subscribed.
        # The 0→1 transition wakes ``wait_for_subscriber`` immediately
        # so the first user to open the dashboard sees fresh ICMP-
        # source state within one sweep instead of waiting up to
        # ``_PING_INTERVAL``. mDNS browsing keeps running
        # unconditionally (it's passive), so devices that announce
        # flip ONLINE the moment the bus delivers their cached state
        # to the new subscriber.
        while True:
            if self._presence is not None:
                await self._presence.wait_for_subscriber()
            await self._resolve_non_api_mdns_targets()
            await self._ping_sweep()
            if self._presence is not None:
                # Interruptible idle wait: bail early if the last
                # subscriber leaves so the next one to connect
                # doesn't sit through the rest of a stale interval.
                # ``wait_for`` raises ``TimeoutError`` after
                # ``_PING_INTERVAL`` when the gate stays open the
                # whole time (the normal "still subscribed, sweep
                # again" path). Either branch loops back to the top
                # where ``wait_for_subscriber`` parks if the gate
                # has since closed.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._presence.wait_for_no_subscribers(),
                        timeout=_PING_INTERVAL,
                    )
                continue
            await asyncio.sleep(_PING_INTERVAL)

    async def _resolve_non_api_mdns_targets(self) -> None:
        """Actively resolve ``.local`` hostnames for non-API devices.

        Devices whose YAML doesn't load the ``api`` integration
        (web_server-only, MQTT-only, OTA-only configs) never
        broadcast on ``_esphomelib._tcp.local.`` so the browser
        callback never fires for them. The cache-based fallback in
        :meth:`_select_ping_targets` only catches them when the
        zeroconf A-record cache happens to be primed (e.g. by an
        unrelated query). On a quiet network where ICMP is also
        filtered (some corporate / HA setups), those devices stay
        UNKNOWN forever even though they're reachable.

        Issue an active mDNS A-record resolve for each non-API
        device every sweep so the indicator flips ONLINE even
        without an esphomelib service announcement. Mirrors the
        legacy dashboard's ``async_refresh_hosts`` poll path
        (``esphome/dashboard/status/mdns.py``). No-op when the
        zeroconf browser failed to start.
        """
        if self._zeroconf is None:
            return
        candidates = [
            d
            for d in self._get_devices()
            if d.address
            and is_local_hostname(d.address)
            and d.loaded_integrations
            and "api" not in d.loaded_integrations
            and self._should_ping(d)
        ]
        if not candidates:
            return
        results = await asyncio.gather(
            *(
                self._zeroconf.async_resolve_host(d.address, _MDNS_HOSTNAME_RESOLVE_TIMEOUT)
                for d in candidates
            ),
            return_exceptions=True,
        )
        for device, addresses in zip(candidates, results, strict=True):
            # Trust mDNS for ONLINE — the active A-record query
            # answered, so the device is live on this LAN. Claim
            # under the ``mdns`` source (priority 3) so the
            # subsequent ICMP sweep skips this device entirely.
            # Keeping ping / DNS traffic to a minimum for fleets
            # that broadcast is a deliberate trade-off: we want
            # mDNS to be the single source of truth for devices
            # that respond to it.
            self._apply_resolved_addresses(device.name, addresses)
            # No OFFLINE branch — deliberate. The browser path can
            # trust mDNS in both directions because the
            # ServiceBrowser delivers a ``Removed`` event when a
            # cached record's TTL expires without renewal; that's
            # the canonical "I'm gone" signal. The one-off active
            # resolve we run here has no such subscription — a
            # miss is just "this single query didn't get a reply
            # in time", which conflates "device gone", "device
            # slow", and "transient packet loss". Falling back to
            # ICMP for the OFFLINE decision in this path is the
            # right shape: an mDNS hit upgrades to mDNS-owned
            # ONLINE; a miss leaves the source slot at whatever
            # ping last claimed (or unknown), and ping decides.

    async def _ping_sweep(self) -> None:
        if icmp_ping is None:
            return

        devices_to_ping = self._select_ping_targets()
        if not devices_to_ping:
            return

        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Pinging %d devices: %s",
                len(devices_to_ping),
                ", ".join(f"{d.name} ({d.address})" for d in devices_to_ping),
            )

        for i in range(0, len(devices_to_ping), _PING_BATCH_SIZE):
            batch = devices_to_ping[i : i + _PING_BATCH_SIZE]
            # Pre-resolve every batch via the DNS cache. icmplib would
            # otherwise re-resolve internally on every ping (going to
            # the system resolver each time and ignoring our cache),
            # and the OTA cache args would have nothing to draw on for
            # non-mDNS hostnames.
            resolved = await asyncio.gather(
                *(self._dns_cache.async_resolve(d.address) for d in batch),
                return_exceptions=True,
            )
            ping_targets: list[tuple[Device, str]] = []
            for device, addresses in zip(batch, resolved, strict=True):
                if isinstance(addresses, list) and addresses:
                    target = addresses[0]
                    # Apply the resolved target so the drawer / table
                    # have an IP to show. ``apply_ip`` already
                    # preserves an existing multi-IP set when the
                    # incoming target is already in it (the typical
                    # case for a ``.local`` host with an active
                    # ``_esphomelib._tcp`` broadcast — the ping
                    # target is the IPv4 primary the browser
                    # callback already populated). For ``.local``
                    # hosts that don't broadcast ``_esphomelib._tcp``
                    # (non-API ESPHome devices, the
                    # zwave-proxy-seeedw5500 case) this is the only
                    # path that ever populates ``device.ip``, so a
                    # ping-source-only device would otherwise show
                    # an em-dash in the drawer's IP row even after
                    # successful pings.
                    self.apply_ip(device.name, target)
                    ping_targets.append((device, target))
                else:
                    # DNS cache says we can't resolve this hostname
                    # (the entry is cached as a failure for the cache
                    # TTL). Don't hand the bare hostname to icmplib —
                    # it would re-resolve via the system resolver every
                    # sweep, hammering DNS for nothing. Treat the cache
                    # miss as the "we tried, can't reach" signal and
                    # apply OFFLINE via the same source ``_ping_device``
                    # would have used.
                    self.apply(device.name, DeviceState.OFFLINE, "ping")
            if ping_targets:
                await asyncio.gather(
                    *(self._ping_device(device, target) for device, target in ping_targets),
                    return_exceptions=True,
                )

    def _select_ping_targets(self) -> list[Device]:
        """
        Filter the device list down to actual ping candidates.

        Devices already known to be ONLINE via a higher-priority source
        are skipped. ``.local`` hosts that show up in zeroconf's cache
        are claimed for mDNS so the bare-hostname DNS fallback can't
        resolve them to an unreachable IP on a different subnet.
        Hostnames with a fresh DNS-failure cache entry are flipped
        OFFLINE without a ping attempt — there's nothing to resolve, so
        re-trying every minute would just hammer the resolver.
        """
        devices_to_ping: list[Device] = []
        dns_skipped: list[Device] = []
        for device in self._get_devices():
            if not device.address or not self._should_ping(device):
                continue
            if is_local_hostname(device.address) and (
                cached := self.get_cached_addresses(device.address)
            ):
                self.apply(device.name, DeviceState.ONLINE, "mdns", claim=True)
                # Forward every cached IP so the dashboard shows all
                # of them; ``apply_ip_addresses`` picks an IPv4 primary
                # for ``device.ip`` so ICMP probes and OTA cache args
                # still hit the cross-subnet-friendly entry.
                self.apply_ip_addresses(device.name, cached)
                continue
            if self._dns_cache.has_cached_failure(device.address):
                dns_skipped.append(device)
                self.apply(device.name, DeviceState.OFFLINE, "ping")
                continue
            devices_to_ping.append(device)

        if dns_skipped and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "Skipping ping for %d device(s) with cached DNS failure: %s",
                len(dns_skipped),
                ", ".join(f"{d.name} ({d.address})" for d in dns_skipped),
            )
        return devices_to_ping

    def _should_ping(self, device: Device) -> bool:
        """
        Decide whether *device* needs an ICMP probe this sweep.

        Mirrors the upstream dashboard's rule: skip the device only when
        it's already ONLINE *and* a higher-priority source (mDNS / MQTT)
        owns it. We still ping devices that are OFFLINE or UNKNOWN so an
        off-network host — one mDNS can't reach because it's on a
        different subnet — has a path to come online via DNS + ping.
        """
        if device.state != DeviceState.ONLINE:
            return True
        source = self._state_source.get(device.name, ReachabilitySource.UNKNOWN)
        return _SOURCE_PRIORITY.get(source, 0) <= _SOURCE_PRIORITY[ReachabilitySource.PING]

    async def _ping_device(self, device: Device, target: str) -> None:
        # Treat any failure mode as "not reachable" → OFFLINE, not as
        # "still unknown". An exception here means resolution failed
        # (NameLookupError), the network refused us (NoRouteToHost,
        # PermissionError, OSError), or icmplib couldn't open a socket.
        # In every case the user wants the dot to flip red, not stay
        # grey forever — once mDNS / MQTT / ping have all tried, the
        # signal is "we couldn't reach this device". A subsequent
        # successful ping will flip it right back to ONLINE.
        rtt_ms: float | None = None
        try:
            result = await icmp_ping(target, count=1, timeout=3, privileged=False)
            is_alive = result.is_alive
            # icmplib's ``Host.min_rtt`` is the lowest round-trip in
            # milliseconds across the count we sent (1 here). Capture
            # it before discarding ``result`` so the drawer can show
            # "4 ms" beside the Ping row. ``min_rtt`` is 0.0 on a
            # failed ping which would surface as "0 ms" — gate on
            # ``is_alive`` so failures stay null.
            if is_alive:
                rtt_ms = float(result.min_rtt)
        except Exception as exc:
            # ``.local`` hosts on systems without Avahi / mdnsd hit
            # this every sweep; the traceback adds nothing and floods
            # the logs. One-line debug is plenty.
            _LOGGER.debug("Ping of %s (%s) failed: %s", device.name, target, exc)
            is_alive = False
        new_state = DeviceState.ONLINE if is_alive else DeviceState.OFFLINE
        if is_alive and rtt_ms is not None and self._reachability is not None:
            self._reachability.record_ping_rtt(device.name, rtt_ms)
        self.apply(device.name, new_state, "ping")


def _pick_ipv4(addresses: list[str]) -> str:
    """
    Return the first IPv4 address in *addresses*, or the first entry overall.

    ``Device.ip`` only carries one IP, so when a host has both V4 and V6
    we lock onto the V4 entry — it's friendlier for ICMP across subnets
    and avoids the IPv6 scope-ID gymnastics that ``apply_ip`` consumers
    aren't prepared for. Callers that need every address (CLI cache args)
    should iterate the list themselves rather than going through this.
    """
    for address in addresses:
        if "." in address and ":" not in address:
            return address
    return addresses[0]
