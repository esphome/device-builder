"""
mDNS source: zeroconf responder, browser, and cache accessors.

:class:`MdnsSource` owns the ``AsyncEsphomeZeroconf`` responder and
the ``AsyncServiceBrowser`` it drives, the esphomelib service-state
callback that reaches into the monitor's apply path, and the
cache-inspection accessors the drawer's reachability snapshot reads.
The HTTP-service / importable-discovery callbacks reach back through
the monitor from the browser's dispatch closure.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from operator import attrgetter
from typing import TYPE_CHECKING, Any

from esphome.zeroconf import AsyncEsphomeZeroconf, DashboardImportDiscovery
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
        # Single browser covers both ``_esphomelib._tcp.local.`` and
        # ``_http._tcp.local.``; the dispatch handler routes events
        # by ``service_type`` to the right per-type logic.
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

        # ``DashboardImportDiscovery`` from upstream esphome owns the
        # TXT-record parsing for adoptable factory firmwares — its
        # ``browser_callback`` only acts on services that carry the
        # ``package_import_url`` TXT records, so harmlessly receiving
        # HTTP events is fine.
        import_discovery = DashboardImportDiscovery(monitor._on_import_update)
        monitor._import_discovery = import_discovery

        def _dispatch(
            zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
        ) -> None:
            # Single ``AsyncServiceBrowser`` covers both service types;
            # dispatch by ``service_type`` so each inner handler only
            # sees the events it cares about. Sharing one browser
            # halves the zeroconf bookkeeping vs running two separate
            # browsers and lets the upstream ``DashboardImportDiscovery``
            # callback piggy-back on the same dispatch path.
            #
            # Closes over the local ``import_discovery`` rather than
            # ``monitor._import_discovery`` so mypy sees a non-None
            # value (the attribute is typed ``... | None`` for the
            # pre-``start()`` window). Same instance either way.
            if service_type == _ESPHOME_SERVICE_TYPE:
                self._on_esphomelib_service_state_change(zeroconf, service_type, name, state_change)
                import_discovery.browser_callback(zeroconf, service_type, name, state_change)
            elif service_type == _HTTP_SERVICE_TYPE:
                monitor._on_http_service_state_change(zeroconf, service_type, name, state_change)

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
        """Cancel the ``AsyncServiceBrowser`` so it stops dispatching new mDNS callbacks.

        Called first during shutdown, BEFORE the monitor drains its
        in-flight resolve tasks — otherwise the browser could spawn
        new resolve tasks during the drain and they'd miss the
        snapshot we took.
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

    def _on_esphomelib_service_state_change(
        self, zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        # ``AsyncServiceBrowser`` dispatches handlers on the asyncio
        # loop, so call apply methods directly. For Added/Updated,
        # try the zeroconf cache first (sync) — only fall back to a
        # network query (async task) when the cache misses.
        monitor = self._monitor
        device_name = device_name_from_service(name)
        _LOGGER.debug("mDNS: %s %s (raw: %s)", state_change, device_name, name)

        # Short-circuit unconfigured devices so we don't spawn
        # ServiceInfo lookups / resolve tasks for unrelated ESPHome
        # nodes on the LAN.
        if monitor._find_device_by_name(device_name) is None:
            return

        if state_change == ServiceStateChange.Removed:
            monitor.apply(device_name, DeviceState.OFFLINE, "mdns")
            monitor.apply_ip(device_name, "")
            monitor.state.state_source.pop(device_name, None)
            if monitor.state.reachability is not None:
                monitor.state.reachability.clear(device_name)
            return

        # ``claim=True`` so mDNS takes ownership even when the
        # device is already ONLINE via a lower-priority source
        # (ping / MQTT), preventing later ping observations from
        # clobbering the now-authoritative mDNS view.
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
        monitor = self._monitor
        # ``claim=True`` so mDNS owns the slot even when ping/MQTT
        # had already labelled the device — same shape the browser
        # callback uses on its way into this method.
        monitor.apply(device_name, DeviceState.ONLINE, "mdns", claim=True)
        # Pull every announced address (IPv4 first, then scoped IPv6
        # — link-local entries keep the ``%scope`` suffix that's
        # required to connect at all). ``apply_ip_addresses`` picks
        # the IPv4 primary for ``device.ip`` and forwards the whole
        # list so ``device.ip_addresses`` reflects what's actually
        # broadcast — a multi-homed dual-stack device used to surface
        # only its V4 here.
        if addresses := info.parsed_scoped_addresses(IPVersion.All):
            monitor.apply_ip_addresses(device_name, addresses)
        # ``decoded_properties`` is a ``dict[str, str | None]`` — zeroconf
        # already handles the UTF-8 decode and None-on-bad-bytes for us.
        props = info.decoded_properties
        if version := props.get("version"):
            monitor.apply_version(device_name, version)
        if config_hash := props.get("config_hash"):
            monitor.apply_config_hash(device_name, config_hash)
        if mac := props.get("mac"):
            monitor.apply_mac_address(device_name, mac)
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
            monitor.apply_api_encryption(device_name, value if isinstance(value, str) else "")
        elif props:
            monitor.apply_api_encryption(device_name, "")

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
        apply_resolved_addresses(self._monitor, name, addresses)

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

    def get_cached_addresses(self, host_name: str) -> list[str] | None:
        """
        Return all zeroconf-cached IPs for *host_name* without issuing a query.

        Both IPv4 and IPv6 (scoped) entries are included — the OTA
        address-cache CLI args need every IP we know so the runtime
        can try them in turn. Callers that want a single best target
        for, say, ICMP should pick IPv4 first themselves.

        Returns ``None`` when zeroconf isn't running, the cache misses,
        or the entry has expired. mDNS-only — see
        :meth:`DeviceStateMonitor.get_cached_dns_addresses` for
        non-``.local`` hostnames.
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
