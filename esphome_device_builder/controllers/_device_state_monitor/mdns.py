"""
mDNS source: zeroconf responder, browser, and cache accessors.

:class:`MdnsSource` owns the ``AsyncEsphomeZeroconf`` responder and
the ``AsyncServiceBrowser`` it drives, the esphomelib service-state
callback that reaches into the monitor's apply path, and the
cache-inspection accessors the drawer's reachability snapshot reads.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from operator import attrgetter
from typing import TYPE_CHECKING, Any

from esphome.zeroconf import AsyncEsphomeZeroconf
from zeroconf import (
    AddressResolver,
    IPVersion,
    ServiceStateChange,
    current_time_millis,
    millis_to_seconds,
)
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo
from zeroconf.const import _CLASS_IN, _TYPE_A, _TYPE_AAAA, _TYPE_SRV, _TYPE_TXT

from ...helpers.hostname import normalize_hostname
from ...models import DeviceState
from .._reachability_tracker import MdnsCacheInfo
from .helpers import (
    _ESPHOME_SERVICE_TYPE,
    _HTTP_SERVICE_TYPE,
    _decode_mdns_txt_records,
    device_name_from_service,
)
from .shared import _MDNS_HOSTNAME_RESOLVE_TIMEOUT, apply_resolved_addresses

if TYPE_CHECKING:
    from .controller import DeviceStateMonitor

_LOGGER = logging.getLogger(__name__)

_MDNS_RESOLVE_TIMEOUT_MS = 2000


class MdnsSource:
    """mDNS source owning the zeroconf responder, browser, and cache accessors."""

    def __init__(self, monitor: DeviceStateMonitor) -> None:
        self._monitor = monitor
        self._zeroconf: AsyncEsphomeZeroconf | None = None
        # Single browser covers both ``_esphomelib._tcp.local.``
        # and ``_http._tcp.local.``; halves the zeroconf
        # bookkeeping versus two parallel browsers.
        self._mdns_browser: AsyncServiceBrowser | None = None

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

        monitor = self._monitor
        importable = monitor._importable
        importable.setup()

        def _dispatch(
            zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
        ) -> None:
            # The shared browser dispatches by service_type so each
            # inner handler only sees the events it cares about,
            # letting the upstream ``DashboardImportDiscovery``
            # piggy-back on the same dispatch path.
            if service_type == _ESPHOME_SERVICE_TYPE:
                self._on_esphomelib_service_state_change(zeroconf, service_type, name, state_change)
                importable.browser_callback(zeroconf, service_type, name, state_change)
            elif service_type == _HTTP_SERVICE_TYPE:
                importable.on_http_service_state_change(zeroconf, service_type, name, state_change)

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
        """Close the zeroconf responder. Called after the resolve-task drain."""
        if self._zeroconf is not None:
            try:
                await self._zeroconf.async_close()
            except Exception:
                _LOGGER.debug("zeroconf close failed", exc_info=True)
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
        records: list[Any] = [
            *self._get_address_records(name),
            *cache.get_all_by_details(service_name, _TYPE_SRV, _CLASS_IN),
            *txt_dns_records,
        ]
        # PTR is owned by the type-domain
        # (``_esphomelib._tcp.local.``) and carries the
        # service-instance as its ``alias``;
        # ``current_entry_with_name_and_alias`` is the
        # zeroconf-API-canonical way to look it up.
        ptr = cache.current_entry_with_name_and_alias(_ESPHOME_SERVICE_TYPE, service_name)
        if ptr is not None:
            records.append(ptr)
        if not records:
            return None
        # Don't filter expired records — the drawer wants the
        # truthful "last seen" age even when the cached record
        # has aged past its TTL.
        now_ms = current_time_millis()
        latest = max(records, key=attrgetter("created"))
        # ``DNSRecord.created`` is millis; ``get_remaining_ttl``
        # already returns seconds (impl divides by 1000.0). Don't
        # convert again — that would turn "108 seconds remaining"
        # into 0.108 and render as "TTL: 0s".
        age_s = max(0.0, millis_to_seconds(now_ms - latest.created))
        ttl_remaining_s = max(0.0, float(latest.get_remaining_ttl(now_ms)))
        return MdnsCacheInfo(
            age_seconds=age_s,
            ttl_remaining_seconds=ttl_remaining_s,
            txt_records=_decode_mdns_txt_records(txt_dns_records),
        )

    def get_cached_addresses(self, host_name: str) -> list[str] | None:
        """
        Return all zeroconf-cached IPs for *host_name* without issuing a query.

        Both IPv4 and IPv6 (scoped) entries are included — the
        OTA address-cache args need every IP we know so the
        runtime can try them in turn. mDNS-only; non-``.local``
        hostnames go through
        :meth:`DeviceStateMonitor.get_cached_dns_addresses`.
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

    def _on_esphomelib_service_state_change(
        self, zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        # ``AsyncServiceBrowser`` dispatches handlers on the
        # asyncio loop, so call apply methods directly. Try the
        # zeroconf cache first (sync) — fall back to a fire-and-
        # forget resolve task on cache miss.
        monitor = self._monitor
        device_name = device_name_from_service(name)
        _LOGGER.debug("mDNS: %s %s (raw: %s)", state_change, device_name, name)

        # Short-circuit unconfigured devices so we don't spawn
        # ServiceInfo lookups for unrelated ESPHome nodes on the LAN.
        if monitor._find_device_by_name(device_name) is None:
            return

        if state_change == ServiceStateChange.Removed:
            monitor.apply(device_name, DeviceState.OFFLINE, "mdns")
            monitor.apply_ip(device_name, "")
            monitor.forget(device_name)
            if monitor.state.reachability is not None:
                monitor.state.reachability.clear(device_name)
            return

        # ``claim=True`` so mDNS takes ownership even when ping
        # or MQTT already labelled the device, blocking the
        # lower-priority sources from clobbering the now-
        # authoritative mDNS view.
        monitor.apply(device_name, DeviceState.ONLINE, "mdns", claim=True)

        info = AsyncServiceInfo(service_type, name)
        if info.load_from_cache(zeroconf):
            self._apply_service_info(device_name, info)
            return

        monitor._track_task(self._resolve_and_apply(zeroconf, info, device_name))

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
        """
        Resolve a cache-miss service and hand the result to *apply*.

        Shared fire-and-forget shape between the esphomelib and
        HTTP browser paths: spawn a task on cache miss,
        ``async_request`` the record, swallow exceptions to a
        debug log, dispatch to the per-type applier on success.
        """
        try:
            if not await info.async_request(zeroconf, timeout=_MDNS_RESOLVE_TIMEOUT_MS):
                return
        except Exception:
            _LOGGER.debug("mDNS resolve failed for %s", device_name, exc_info=True)
            return
        apply(device_name, info)

    def _apply_service_info(self, device_name: str, info: AsyncServiceInfo) -> None:
        """
        Pull IP / version / config_hash / encryption off a populated ``AsyncServiceInfo``.

        Claims ONLINE under the mDNS source — the browser-
        callback path has already claimed but ``probe_device``
        skips that prelude, so the dedupe-vs-fresh-claim happens
        here.
        """
        monitor = self._monitor
        monitor.apply(device_name, DeviceState.ONLINE, "mdns", claim=True)
        # Pass the full announced address set (IPv4 first, then
        # scoped IPv6 — link-local entries keep the ``%scope``
        # suffix). ``apply_ip_addresses`` picks the IPv4 primary
        # but forwards everything so multi-homed dual-stack
        # devices surface every IP.
        if addresses := info.parsed_scoped_addresses(IPVersion.All):
            monitor.apply_ip_addresses(device_name, addresses)
        props = info.decoded_properties
        if version := props.get("version"):
            monitor.apply_version(device_name, version)
        if config_hash := props.get("config_hash"):
            monitor.apply_config_hash(device_name, config_hash)
        if mac := props.get("mac"):
            monitor.apply_mac_address(device_name, mac)
        # api_encryption tri-state semantics on this announce.
        # The four cases are load-bearing — narrative dropped,
        # but the case enumeration captures the empty-string-
        # means-plaintext-confirmed contract documented in
        # CLAUDE.md ("Things that have bitten us"):
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

    def _get_address_records(self, name: str) -> list[Any]:
        """Return cached A and AAAA records for *name*, or ``[]``."""
        if self._zeroconf is None:
            return []
        cache = self._zeroconf.zeroconf.cache
        local_name = f"{name}.local."
        return [
            *cache.get_all_by_details(local_name, _TYPE_A, _CLASS_IN),
            *cache.get_all_by_details(local_name, _TYPE_AAAA, _CLASS_IN),
        ]
