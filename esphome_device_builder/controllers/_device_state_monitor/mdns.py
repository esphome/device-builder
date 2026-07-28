"""
mDNS source: zeroconf responder, browser, and cache accessors.

:class:`MdnsSource` owns the ``AsyncEsphomeZeroconf`` responder and
the ``AsyncServiceBrowser`` it drives, the esphomelib service-state
callback that reaches into the monitor's apply path, the ``_http._tcp``
fallback callback that reads a non-API device's identity TXT, and the
cache-inspection accessors the drawer's reachability snapshot reads.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from functools import partial
from operator import attrgetter
from typing import TYPE_CHECKING, Any, cast

from esphome.zeroconf import AsyncEsphomeZeroconf
from zeroconf import (
    AddressResolver,
    DNSPointer,
    DNSRecord,
    IPVersion,
    ServiceStateChange,
    current_time_millis,
    millis_to_seconds,
)
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo
from zeroconf.const import _CLASS_IN, _TYPE_A, _TYPE_AAAA, _TYPE_PTR, _TYPE_SRV, _TYPE_TXT

from ...helpers.async_ import drain_tasks, log_task_exit
from ...helpers.hostname import normalize_hostname
from ...helpers.ip import drop_unspecified_addresses
from ...models import DeviceState
from .._reachability_tracker import MdnsCacheInfo
from .helpers import (
    _ESPHOME_SERVICE_TYPE,
    _HTTP_SERVICE_TYPE,
    _decode_mdns_txt_records,
    device_name_from_service,
)
from .interface_monitor import monitor_interfaces
from .shared import _MDNS_HOSTNAME_RESOLVE_TIMEOUT, apply_resolved_addresses

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)

# Matches upstream esphome's ``zeroconf.DEFAULT_TIMEOUT`` — slow ESP32/ESP8266
# nodes routinely need most of it, and ``async_request`` keeps re-querying the
# wire inside the window, so a short one-shot silently drops their announce.
_MDNS_RESOLVE_TIMEOUT_MS = 10_000

# The ping sweep's resolve-first pass runs before the ICMP sweep it feeds, so
# it gets the same 3s bound as the non-API active resolve — a slow node the
# window misses is pinged this sweep and re-resolved next.
_SWEEP_RESOLVE_TIMEOUT_MS = 3_000

# Bound on the zeroconf close. ``async_close`` broadcasts mDNS goodbyes and can
# hang on a wedged socket; shutdown must not block on it.
_MDNS_CLOSE_TIMEOUT = 1.0

# Padding added to the cached A record's TTL when the drawer's
# refresh loop schedules its next probe. Sleeping ``ttl + this``
# guarantees ``async_resolve_host`` falls through its cache short-
# circuit and actually goes on the wire (see ``refresh_mdns``).
_MDNS_REFRESH_PADDING_SECONDS = 1.0

# Identity TXT key → monitor applier, the single source for both the
# apply loop and the presence check. Lambdas, not partials / bound
# methods: ``DeviceStateMonitor`` is TYPE_CHECKING-only here (circular
# import), and the call-time attribute lookup keeps instance-level
# overrides (tests) working like the inline calls they replaced. The
# ``deployed_identity_live`` flag keys on their presence: a contentless
# ``_http._tcp`` service (an api+web_server device, or old web_server
# firmware) must never vouch for the identity trio, or a flag-True
# device with an identity-less cached TXT would verify-resolve every
# sweep forever.
_IDENTITY_TXT_APPLIERS: tuple[tuple[str, Callable[[DeviceStateMonitor, str, str], bool]], ...] = (
    ("version", lambda monitor, name, value: monitor.apply_version(name, value)),
    ("config_hash", lambda monitor, name, value: monitor.apply_config_hash(name, value)),
    ("mac", lambda monitor, name, value: monitor.apply_mac_address(name, value)),
)


def _has_identity_keys(props: Mapping[str, str | None]) -> bool:
    """Whether *props* carries any identity TXT key with a value."""
    return any(props.get(key) for key, _apply in _IDENTITY_TXT_APPLIERS)


class MdnsSource:
    """mDNS source owning the zeroconf responder, browser, and cache accessors."""

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor
        self._zeroconf: AsyncEsphomeZeroconf | None = None
        # Single browser covers both ``_esphomelib._tcp.local.``
        # and ``_http._tcp.local.``; halves the zeroconf
        # bookkeeping versus two parallel browsers.
        self._mdns_browser: AsyncServiceBrowser | None = None
        self._interface_monitor_task: asyncio.Task[None] | None = None
        # Full service names with a wire resolve in flight. Browser event
        # churn on a still-unresolved service (Added then Updated) would
        # otherwise stack concurrent resolvers, each holding a global
        # zeroconf listener for the whole resolve window.
        self._inflight_resolves: set[str] = set()

    @property
    def zeroconf(self) -> AsyncEsphomeZeroconf | None:
        """The mDNS responder, or ``None`` when zeroconf failed to start."""
        return self._zeroconf

    async def start(self) -> None:
        try:
            self._zeroconf = AsyncEsphomeZeroconf()
        except Exception:
            _LOGGER.exception("Could not start zeroconf — falling back to ping only")
            self._zeroconf = None
            return

        try:
            self._mdns_browser = AsyncServiceBrowser(
                self._zeroconf.zeroconf,
                [_ESPHOME_SERVICE_TYPE, _HTTP_SERVICE_TYPE],
                handlers=[self._on_browser_event],
            )
            _LOGGER.info(
                "mDNS browser started for %s, %s",
                _ESPHOME_SERVICE_TYPE,
                _HTTP_SERVICE_TYPE,
            )
        except Exception:
            _LOGGER.exception("Could not start mDNS browser — device discovery limited to ping")

        # Keep the responder bound to the live interface set (VPN / Wi-Fi /
        # Docker churn) for the instance's lifetime; cancelled in close_zeroconf.
        if self._zeroconf is not None:
            self._interface_monitor_task = asyncio.create_task(monitor_interfaces(self._zeroconf))
            self._interface_monitor_task.add_done_callback(
                partial(log_task_exit, "Interface monitor")
            )

    async def cancel_browser(self) -> None:
        """
        Cancel the ``AsyncServiceBrowser``.

        Must run BEFORE the monitor's resolve-task drain — otherwise
        the browser could spawn new resolve tasks during the drain
        and they'd miss the snapshot.
        """
        if self._mdns_browser is not None:
            try:
                await self._mdns_browser.async_cancel()
            except Exception:
                _LOGGER.debug("mDNS browser cancel failed", exc_info=True)
            self._mdns_browser = None

    async def close_zeroconf(self) -> None:
        """Close the zeroconf responder, bounded so a wedged socket can't stall shutdown."""
        # Stop the interface monitor first so it can't reconcile a closing instance.
        if self._interface_monitor_task is not None:
            # A crashed monitor task must not abort shutdown before zeroconf closes.
            await drain_tasks([self._interface_monitor_task], log_exceptions=True)
            self._interface_monitor_task = None
        if self._zeroconf is not None:
            try:
                await asyncio.wait_for(self._zeroconf.async_close(), _MDNS_CLOSE_TIMEOUT)
            except Exception:
                _LOGGER.debug("zeroconf close failed or timed out", exc_info=True)
            self._zeroconf = None

    async def refresh_mdns(self, name: str) -> None:
        """
        Re-query a device's mDNS A/AAAA records via the wire.

        ESPHome devices are mDNS-silent except in response to
        probes, so this is the only mechanism that keeps an
        A record alive once it ages out — the browser's PTR
        (4500s TTL) stays fresh but A (120s) decays on its own.
        Caller must schedule this *after* the cached A's TTL
        elapses or ``async_resolve_host``'s cache short-circuit
        will swallow the call without going on the wire.
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
        apply_resolved_addresses(self._monitor, name, addresses)

    def get_mdns_a_record_ttl_remaining(self, name: str) -> float | None:
        """
        Return the minimum remaining TTL across cached A/AAAA records.

        Scoped to A/AAAA (not the union ``get_mdns_cache_info``
        walks) because the drawer's refresh loop needs the
        A-specific expiry — sleeping on the PTR's 4500s TTL
        would never trigger the wire-query refresh the loop
        exists for.
        """
        records = self._get_address_records(name)
        if not records:
            return None
        now_ms = current_time_millis()
        return max(0.0, min(float(r.get_remaining_ttl(now_ms)) for r in records))

    def get_mdns_cache_info(self, name: str) -> MdnsCacheInfo | None:
        """
        Read the truthful "last heard via mDNS" age + remaining TTL.

        Walks every record type the device might leave in the
        cache (A / AAAA at ``<name>.local.``, SRV / TXT at
        ``<name>._esphomelib._tcp.local.``, PTR at the type-
        domain). The drawer's "Last seen" reads whichever is
        freshest: A/AAAA decay at 120s, but the PTR kept alive
        by the browser stays fresh for tens of minutes, so the
        row stays populated through the brief A-expiry window
        instead of flickering "Waiting for first broadcast".
        Returns ``None`` only when *every* cached record has
        been evicted.
        """
        if self._zeroconf is None:
            return None
        cache = self._zeroconf.zeroconf.cache
        service_name = f"{name}.{_ESPHOME_SERVICE_TYPE}"
        txt_dns_records = list(cache.get_all_by_details(service_name, _TYPE_TXT, _CLASS_IN))
        records: list[DNSRecord] = [
            *self._get_address_records(name),
            *cache.get_all_by_details(service_name, _TYPE_SRV, _CLASS_IN),
            *txt_dns_records,
        ]
        ptr = self._cached_ptr(service_name)
        if ptr is not None:
            records.append(ptr)
        if not records:
            return None
        # Don't filter expired A/AAAA/SRV/TXT records — the drawer
        # wants the truthful "last seen" age even when the cached
        # record has aged past its TTL. (The PTR lookup alone is
        # live-only; zeroconf's alias API filters expired entries.)
        now_ms = current_time_millis()
        latest = max(records, key=attrgetter("created"))
        # ``DNSRecord.created`` is millis; ``get_remaining_ttl``
        # already returns seconds (impl divides by 1000.0). Don't
        # convert again — that would turn "108 seconds remaining"
        # into 0.108 and render as "TTL: 0s".
        age_s = max(0.0, millis_to_seconds(now_ms - latest.created))
        ttl_remaining_s = max(0.0, float(latest.get_remaining_ttl(now_ms)))
        # The PTR's full announced TTL (the device's own record
        # lifetime). The drawer's "offline in N" countdown is this
        # lifetime measured from ``age_seconds`` so it stays in
        # lockstep with "last seen"; the PTR's *remaining* TTL is
        # refreshed by the browser at ~80% of the lifetime and would
        # drift against the actively-probed A record.
        ptr_ttl_s = float(ptr.ttl) if ptr is not None else None
        return MdnsCacheInfo(
            age_seconds=age_s,
            ttl_remaining_seconds=ttl_remaining_s,
            ptr_ttl_seconds=ptr_ttl_s,
            txt_records=_decode_mdns_txt_records(txt_dns_records),
        )

    def get_cached_addresses(self, host_name: str) -> list[str] | None:
        """
        Return all zeroconf-cached IPs for *host_name* without issuing a query.

        Both IPv4 and IPv6 (scoped) entries are included — the
        OTA address-cache args need every IP we know so the
        runtime can try them in turn. Unspecified entries (a junk
        ``0.0.0.0`` / ``::`` announce) are dropped. mDNS-only;
        non-``.local`` hostnames go through
        ``state.dns_cache.get_cached_addresses``.
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
        return drop_unspecified_addresses(addresses) or None

    def reconcile_from_cache(self, device_name: str) -> None:
        """
        Re-apply the cached TXT payload without claiming state or IP.

        Level-triggered repair for the edge-triggered apply path: a
        drop window (cold-start probe no-op, timed-out resolve) leaves
        the record blank while the cache holds the TXT, and zeroconf's
        same-content TTL refreshes never re-fire the browser handler.
        No ONLINE claim — a cache hit can be stale, and a claim here
        has no browser ``Removed`` counterpart (#1776).
        """
        # Read the TXT record directly rather than via
        # ``AsyncServiceInfo.load_from_cache``, whose success requires an
        # unexpired *address* record — the A (120s TTL) routinely expires
        # while the TXT (4500s) is still cached, exactly the state this
        # pass repairs.
        if props := self._cached_txt_properties(f"{device_name}.{_ESPHOME_SERVICE_TYPE}"):
            self._apply_txt_properties(device_name, props)
        # The ``_http._tcp`` identity TXT exists only when the API is
        # absent, so no bucket gate is needed; api_encryption stays
        # untouched (see ``_apply_http_txt``).
        if props := self._cached_txt_properties(f"{device_name}.{_HTTP_SERVICE_TYPE}"):
            self._apply_http_identity_props(device_name, props)

    async def resolve_and_claim(self, device_name: str) -> None:
        """Resolve the esphomelib service (cache first, wire fallback) and claim mdns on a hit."""
        if (zc := self._zeroconf) is None:
            return
        info = AsyncServiceInfo(_ESPHOME_SERVICE_TYPE, f"{device_name}.{_ESPHOME_SERVICE_TYPE}")
        if info.load_from_cache(zc.zeroconf):
            self._apply_service_info(device_name, info)
            return
        await self.resolve_then(
            zc.zeroconf,
            info,
            device_name,
            self._apply_service_info,
            timeout_ms=_SWEEP_RESOLVE_TIMEOUT_MS,
        )

    def has_live_ptr(self, device_name: str) -> bool:
        """Whether the cache holds an unexpired esphomelib PTR for *device_name*."""
        return self._cached_ptr(f"{device_name}.{_ESPHOME_SERVICE_TYPE}") is not None

    def has_live_http_identity_txt(self, device_name: str) -> bool:
        """Whether the cache holds an unexpired ``_http._tcp`` TXT carrying identity keys."""
        return _has_identity_keys(
            self._cached_txt_properties(f"{device_name}.{_HTTP_SERVICE_TYPE}")
        )

    async def verify_http_identity(self, device_name: str) -> None:
        """
        Re-resolve the ``_http._tcp`` service before dropping identity freshness.

        Only a confirmed miss, or an answer provably lacking the identity
        keys, clears the flag; no verdict leaves it — never demote on
        uncertainty.
        """
        if (zc := self._zeroconf) is None:
            return
        info = AsyncServiceInfo(_HTTP_SERVICE_TYPE, f"{device_name}.{_HTTP_SERVICE_TYPE}")
        verdict = await self.resolve_then(
            zc.zeroconf,
            info,
            device_name,
            self._apply_http_txt,
            timeout_ms=_SWEEP_RESOLVE_TIMEOUT_MS,
        )
        if verdict is None:
            return
        if verdict and _has_identity_keys(info.decoded_properties):
            return
        self._monitor.apply_deployed_identity_live(device_name, live=False)

    def live_ptr_service_names(self) -> set[str]:
        """
        Snapshot the unexpired esphomelib PTR aliases in the cache.

        Membership key is ``f"{name}.{_ESPHOME_SERVICE_TYPE}"``.
        """
        if self._zeroconf is None:
            return set()
        now_ms = current_time_millis()
        return {
            cast(DNSPointer, record).alias
            for record in self._zeroconf.zeroconf.cache.get_all_by_details(
                _ESPHOME_SERVICE_TYPE, _TYPE_PTR, _CLASS_IN
            )
            if not record.is_expired(now_ms)
        }

    def has_cached_trace(self, name: str, service_type: str = _ESPHOME_SERVICE_TYPE) -> bool:
        """
        Whether the cache holds any record for *name*, expired included.

        Same record buckets as :meth:`get_mdns_cache_info`; keep the
        two in lockstep. Sweep-side verify resolves gate on it: an
        mDNS-dark deployment leaves no trace, and a wire miss there
        proves nothing. *service_type* narrows only the
        service-instance buckets — cached A/AAAA records count as a
        multicast trace for any service type.
        """
        if self._zeroconf is None:
            return False
        if self._get_address_records(name):
            return True
        cache = self._zeroconf.zeroconf.cache
        service_name = f"{name}.{service_type}"
        return bool(
            cache.get_all_by_details(service_name, _TYPE_SRV, _CLASS_IN)
            or cache.get_all_by_details(service_name, _TYPE_TXT, _CLASS_IN)
            or self._cached_ptr(service_name, service_type) is not None
        )

    def probe_device(self, device_name: str, service_name: str | None = None) -> None:
        """
        Eagerly resolve a device's ``_esphomelib._tcp.local.`` service.

        Short-circuits the post-adoption wait for the next mDNS
        announce — flips the card from "Unknown" to fully-populated
        immediately by reading the zeroconf cache (sync hit) or
        kicking off a fire-and-forget ``async_request``.

        ``service_name`` defaults to ``device_name``; pass it
        explicitly when the device's mDNS-advertised name (its
        original factory-firmware hostname) differs from the
        user-chosen YAML name so the lookup hits the cache while
        the apply still keys to the configured name.
        """
        if (zc := self._zeroconf) is None:
            return
        broadcast = service_name or device_name
        info = AsyncServiceInfo(_ESPHOME_SERVICE_TYPE, f"{broadcast}.{_ESPHOME_SERVICE_TYPE}")
        self.cache_apply_or_resolve(zc.zeroconf, info, device_name)

    async def resolve_then(
        self,
        zeroconf: Any,
        info: AsyncServiceInfo,
        device_name: str,
        apply: Callable[[str, AsyncServiceInfo], None],
        *,
        timeout_ms: float = _MDNS_RESOLVE_TIMEOUT_MS,
    ) -> bool | None:
        """
        Resolve a cache-miss service and hand the result to *apply*.

        ``async_request`` the record, swallow exceptions to a
        debug log, dispatch to the per-type applier on success.
        At most one resolve per service name is in flight. Returns
        True when the service resolved and *apply* ran, False on a
        confirmed miss, None when there is no verdict (a swallowed
        error, or a resolve already in flight).
        """
        if info.name in self._inflight_resolves:
            return None
        self._inflight_resolves.add(info.name)
        try:
            if not await info.async_request(zeroconf, timeout=timeout_ms):
                return False
        except Exception:
            _LOGGER.debug("mDNS resolve failed for %s", device_name, exc_info=True)
            return None
        finally:
            self._inflight_resolves.discard(info.name)
        apply(device_name, info)
        return True

    def cache_apply_or_resolve(
        self,
        zeroconf: Any,
        info: AsyncServiceInfo,
        device_name: str,
        apply: Callable[[str, AsyncServiceInfo], None] | None = None,
    ) -> None:
        """Apply *info* synchronously off the zeroconf cache, else resolve fire-and-forget."""
        applier = apply or self._apply_service_info
        if info.load_from_cache(zeroconf):
            applier(device_name, info)
            return
        self._monitor._track_task(self.resolve_then(zeroconf, info, device_name, applier))

    def _on_esphomelib_service_state_change(
        self, zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        # ``AsyncServiceBrowser`` dispatches handlers on the
        # asyncio loop, so call apply methods directly.
        monitor = self._monitor
        device_name = device_name_from_service(name)
        _LOGGER.debug("mDNS: %s %s (raw: %s)", state_change, device_name, name)

        # Short-circuit unconfigured devices so we don't spawn
        # ServiceInfo lookups for unrelated ESPHome nodes on the LAN.
        if monitor._find_device_by_name(device_name) is None:
            return

        if state_change == ServiceStateChange.Removed:
            self._on_service_removed(device_name)
            return

        # Don't claim ONLINE off a bare PTR — only once the service
        # actually resolves (cache hit below, or the wire resolve).
        # A PTR with no resolvable SRV/A is not a reachable device
        # (a node that died mid-handshake, or a reflector re-serving
        # a stale PTR for a long-gone device); claiming here latched
        # it ONLINE forever with no IP, locking out the ICMP sweep.
        info = AsyncServiceInfo(service_type, name)
        self.cache_apply_or_resolve(zeroconf, info, device_name)

    def _on_browser_event(
        self, zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        # The shared browser dispatches by service_type so each
        # inner handler only sees the events it cares about,
        # letting the upstream ``DashboardImportDiscovery``
        # piggy-back on the same dispatch path.
        importable = self._monitor.importable
        if service_type == _ESPHOME_SERVICE_TYPE:
            self._on_esphomelib_service_state_change(zeroconf, service_type, name, state_change)
            importable.browser_callback(zeroconf, service_type, name, state_change)
        elif service_type == _HTTP_SERVICE_TYPE:
            self._on_http_service_state_change(zeroconf, service_type, name, state_change)
            importable.on_http_service_state_change(zeroconf, service_type, name, state_change)

    def _on_service_removed(self, device_name: str) -> None:
        """Mark a withdrawn service UNKNOWN and wake the ping sweep to decide."""
        # A goodbye withdraws only the PTR (firmware never byes SRV/A,
        # which stay cached for their full TTLs), so a verify-resolve
        # here would vouch for a sleeping device straight off the cache
        # and latch it ONLINE (#2369, #1776).
        monitor = self._monitor
        monitor.source_withdrawn(device_name, "mdns")
        monitor.probe_device_ping(device_name)

    def _apply_service_info(self, device_name: str, info: AsyncServiceInfo) -> None:
        """
        Pull IP / version / config_hash / encryption off a populated ``AsyncServiceInfo``.

        The single ONLINE-claim point for the mDNS source: reached
        only once a service actually resolved (browser cache hit,
        wire resolve, or ``probe_device``), so an unresolvable
        announce never claims.
        """
        monitor = self._monitor
        # Claimed before the apply-path unspecified-address filter, unlike
        # the active-resolve path: a resolved service is liveness evidence
        # on its own (already claimed even when addressless), and the
        # browser's ``Removed`` lifecycle withdraws the claim so ping
        # decides — no permanent latch.
        monitor.apply(device_name, DeviceState.ONLINE, "mdns", claim=True)
        # Pass the full announced address set (IPv4 first, then
        # scoped IPv6 — link-local entries keep the ``%scope``
        # suffix). ``apply_ip_addresses`` picks the IPv4 primary
        # but forwards everything so multi-homed dual-stack
        # devices surface every IP.
        if addresses := info.parsed_scoped_addresses(IPVersion.All):
            monitor.apply_ip_addresses(device_name, addresses)
        self._apply_txt_properties(device_name, info.decoded_properties)

    def _apply_txt_properties(self, device_name: str, props: Mapping[str, str | None]) -> None:
        """Apply version / config_hash / mac / api_encryption from decoded TXT properties."""
        monitor = self._monitor
        self._apply_identity_txt(device_name, props)
        # api_encryption tri-state semantics on this announce:
        #
        # * Key present with truthy value: encryption confirmed
        #   live → apply with that string.
        # * Key present with empty / bare-key value (zeroconf
        #   collapses both to ``None``): device explicitly
        #   broadcast "no key" → apply with ``""``.
        # * Key absent AND props carries other content
        #   (``version`` / ``mac`` / ``config_hash`` / ...):
        #   firmware rebuilt without encryption — apply ``""``
        #   so the indicator follows the wire. TXT broadcasts
        #   are atomic per announce, so a content-bearing TXT
        #   without the key is authoritative for "encryption
        #   was removed".
        # * Key absent AND props empty: preserve — the cache-
        #   eviction / truly-empty-fragment shape.
        if "api_encryption" in props:
            value = props["api_encryption"]
            monitor.apply_api_encryption(device_name, value if isinstance(value, str) else "")
        elif props:
            monitor.apply_api_encryption(device_name, "")

    def _apply_identity_txt(self, device_name: str, props: Mapping[str, str | None]) -> None:
        """Apply the version / config_hash / mac identity TXT keys, tolerating absence."""
        monitor = self._monitor
        for key, apply in _IDENTITY_TXT_APPLIERS:
            if value := props.get(key):
                apply(monitor, device_name, value)

    def _on_http_service_state_change(
        self, zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        """
        Read the identity TXT off a non-API device's ``_http._tcp`` service.

        New firmware publishes version / mac / config_hash on both the
        fallback and web_server's service when the API is absent;
        older firmware carries ``version`` only.
        Skipped when every config for the name exposes the API (the
        esphomelib path carries their identity, and the ``_http`` TXT
        isn't published with the API on). No ONLINE claim; reachability
        stays owned by the active-resolve / MQTT / ping paths.
        """
        if state_change == ServiceStateChange.Removed:
            return
        monitor = self._monitor
        device_name = device_name_from_service(name)
        # Look at the whole name bucket, not just bucket[0]: sibling
        # YAMLs can share an ``esphome.name`` (a config + a ``foo (1)``
        # copy), and an all-API bucket is the only one to skip.
        bucket = monitor._get_devices_by_name(device_name)
        if not bucket or all(device.api_enabled for device in bucket):
            return
        info = AsyncServiceInfo(service_type, name)
        self.cache_apply_or_resolve(zeroconf, info, device_name, self._apply_http_txt)

    def _apply_http_txt(self, device_name: str, info: AsyncServiceInfo) -> None:
        """
        Apply the identity TXT from a resolved ``_http._tcp`` service.

        Identity keys only — never api_encryption: a device without the
        API has no encryption state, and the absent-key-means-plaintext
        rule from the esphomelib path would stamp a false confirmation.
        """
        self._apply_http_identity_props(device_name, info.decoded_properties)

    def _apply_http_identity_props(self, device_name: str, props: Mapping[str, str | None]) -> None:
        """Apply ``_http._tcp`` identity keys and stamp freshness when any are present."""
        self._apply_identity_txt(device_name, props)
        if _has_identity_keys(props):
            self._monitor.apply_deployed_identity_live(device_name, live=True)

    def _cached_ptr(
        self, service_name: str, service_type: str = _ESPHOME_SERVICE_TYPE
    ) -> DNSRecord | None:
        """
        Look up the live (unexpired) cached PTR for *service_name*.

        PTR is owned by the type-domain (``_esphomelib._tcp.local.``) and
        carries the service-instance as its ``alias``;
        ``current_entry_with_name_and_alias`` is the zeroconf-API-canonical
        way to look it up, and it filters expired entries itself.
        """
        if self._zeroconf is None:
            return None
        ptr: DNSRecord | None = self._zeroconf.zeroconf.cache.current_entry_with_name_and_alias(
            service_type, service_name
        )
        return ptr

    def _cached_txt_properties(self, service_name: str) -> dict[str, str]:
        """Decode the unexpired cached TXT records for *service_name*."""
        if self._zeroconf is None:
            return {}
        now_ms = current_time_millis()
        records = [
            record
            for record in self._zeroconf.zeroconf.cache.get_all_by_details(
                service_name, _TYPE_TXT, _CLASS_IN
            )
            if not record.is_expired(now_ms)
        ]
        return _decode_mdns_txt_records(records)

    def _get_address_records(self, name: str) -> list[DNSRecord]:
        """Return cached A and AAAA records for *name*, or ``[]``."""
        if self._zeroconf is None:
            return []
        cache = self._zeroconf.zeroconf.cache
        local_name = f"{name}.local."
        return [
            *cache.get_all_by_details(local_name, _TYPE_A, _CLASS_IN),
            *cache.get_all_by_details(local_name, _TYPE_AAAA, _CLASS_IN),
        ]
