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
import logging
from collections.abc import Callable
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

from ..helpers.hostname import is_local_hostname, normalize_hostname
from ..models import AdoptableDevice, Device, DeviceState
from ._dns_cache import DNSCache

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

# Source priority for state observations. A new observation can only
# override an existing one when its priority is greater than or equal
# to the current source's. Keep ``unknown`` at zero so any source can
# claim a device that no source has yet labelled.
_SOURCE_PRIORITY = {"unknown": 0, "ping": 1, "mqtt": 2, "mdns": 3}

# Callback signature used by DeviceStateMonitor to push state changes
# back to its owner. The owner decides what to do with the new state
# (e.g. fire a bus event, mutate the device model).
StateChangeCallback = Callable[[str, DeviceState, str], None]

# Callback fired when mDNS resolves (or clears) a device's IP address.
# Empty string signals the device went offline / was removed from mDNS.
IPChangeCallback = Callable[[str, str], None]

# Callback fired when the mDNS ``version`` TXT record reports a
# different firmware version than last seen for a device.
VersionChangeCallback = Callable[[str, str], None]

# Callback fired when the mDNS ``config_hash`` TXT record reports a
# different running-config hash than last seen for a device. The hash
# is the 8-char lowercase hex of ``App.get_config_hash()`` and is only
# broadcast by firmware built from esphome/esphome#16145 onwards;
# older devices simply never fire this callback.
ConfigHashChangeCallback = Callable[[str, str], None]

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
        on_importable_added: ImportableAddedCallback | None = None,
        on_importable_removed: ImportableRemovedCallback | None = None,
        is_ignored: Callable[[str], bool] | None = None,
    ) -> None:
        self._get_devices = get_devices
        self._on_state_change = on_state_change
        self._on_ip_change = on_ip_change
        self._on_version_change = on_version_change
        self._on_config_hash_change = on_config_hash_change
        self._on_importable_added = on_importable_added
        self._on_importable_removed = on_importable_removed
        self._is_ignored = is_ignored or (lambda _name: False)
        self._state_source: dict[str, str] = {}  # device name → "mdns" | "ping"
        self._device_ips: dict[str, str] = {}  # device name → last known IP
        self._device_versions: dict[str, str] = {}  # device name → last reported version
        self._device_config_hashes: dict[str, str] = {}  # device name → last reported config hash
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

    def priority_for(self, name: str) -> str:
        """Return the source currently authoritative for *name* (or "unknown")."""
        return self._state_source.get(name, "unknown")

    def apply(self, name: str, state: DeviceState, source: str, *, claim: bool = False) -> bool:
        """
        Record a state observation from *source*.

        Returns True when the observation actually changed the device's
        state and the change was forwarded to the callback. Sources
        below the current source's priority are ignored; same-state
        observations are no-ops.

        ``claim=True`` lets *source* take ownership of the device's
        state slot even when the state is unchanged, so that a
        higher-priority observation arriving after a lower-priority
        one already pinned the same state can still prevent the
        lower-priority source from later flipping it back. The
        priority check still applies — ``claim`` doesn't let a lower-
        priority source override a higher-priority owner.
        """
        device = self._find_device_by_name(name)
        if device is None:
            _LOGGER.debug(
                "Device %s not in catalog — ignoring %s state from %s", name, state, source
            )
            return False

        current_source = self._state_source.get(name, "unknown")
        if _SOURCE_PRIORITY.get(source, 0) < _SOURCE_PRIORITY.get(current_source, 0):
            return False
        if device.state == state:
            if claim:
                self._state_source[name] = source
            return False

        self._state_source[name] = source
        self._on_state_change(name, state, source)
        return True

    def apply_ip(self, name: str, ip: str) -> bool:
        """
        Record an IP observation. Empty string clears the stored IP.

        Returns True when the IP actually changed and the change was
        forwarded to the callback.
        """
        if self._find_device_by_name(name) is None:
            return False
        prev = self._device_ips.get(name, "")
        if prev == ip:
            return False
        if ip:
            self._device_ips[name] = ip
        else:
            self._device_ips.pop(name, None)
        self._on_ip_change(name, ip)
        return True

    def apply_version(self, name: str, version: str) -> bool:
        """
        Record a firmware version observation.

        Returns True when the version actually changed and the change
        was forwarded to the callback.
        """
        if not version or self._on_version_change is None:
            return False
        if self._find_device_by_name(name) is None:
            return False
        if self._device_versions.get(name) == version:
            return False
        self._device_versions[name] = version
        self._on_version_change(name, version)
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
        if self._find_device_by_name(name) is None:
            return False
        if self._device_config_hashes.get(name) == config_hash:
            return False
        self._device_config_hashes[name] = config_hash
        self._on_config_hash_change(name, config_hash)
        return True

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
        for device in self._get_devices():
            if device.name == name:
                return device
        return None

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
        """Pull IP / version / config_hash off a populated ``AsyncServiceInfo``."""
        # Prefer V4; fall back to scoped V6 (link-local needs the
        # ``%scope`` suffix to connect at all). Matches the upstream
        # esphome dashboard's ``parsed_scoped_addresses`` usage.
        addresses = info.parsed_scoped_addresses(IPVersion.V4Only) or info.parsed_scoped_addresses(
            IPVersion.V6Only
        )
        if addresses:
            self.apply_ip(device_name, addresses[0])
        # ``decoded_properties`` is a ``dict[str, str | None]`` — zeroconf
        # already handles the UTF-8 decode and None-on-bad-bytes for us.
        props = info.decoded_properties
        if version := props.get("version"):
            self.apply_version(device_name, version)
        if config_hash := props.get("config_hash"):
            self.apply_config_hash(device_name, config_hash)

    async def _ping_loop(self) -> None:
        # First sweep after the short bootstrap window — gives mDNS a
        # head start so we don't redundantly ping devices the browser
        # is about to flip ONLINE for free, but still gets the UNKNOWN
        # → OFFLINE transition in front of the user within ~10s of
        # startup instead of after a full minute.
        try:
            await asyncio.sleep(_PING_BOOTSTRAP_DELAY)
            await self._ping_sweep()
            while True:
                await asyncio.sleep(_PING_INTERVAL)
                await self._ping_sweep()
        except asyncio.CancelledError:
            pass

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
                    # mDNS owns IP tracking for ``.local`` hosts; only
                    # backfill from DNS for non-mDNS hosts so a stale
                    # DNS result can't clobber the live mDNS value.
                    if not is_local_hostname(device.address):
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
                # Prefer IPv4 for ``apply_ip`` (the per-device single
                # IP) so the device-list display and any ad-hoc ICMP
                # probe both pick the cross-subnet-friendly entry. The
                # OTA cache args built in
                # ``_build_address_cache_args`` consume every cached
                # IP separately, so we don't lose V6 reachability by
                # picking V4 here.
                self.apply_ip(device.name, _pick_ipv4(cached))
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
        source = self._state_source.get(device.name, "unknown")
        return _SOURCE_PRIORITY.get(source, 0) <= _SOURCE_PRIORITY["ping"]

    async def _ping_device(self, device: Device, target: str) -> None:
        # Treat any failure mode as "not reachable" → OFFLINE, not as
        # "still unknown". An exception here means resolution failed
        # (NameLookupError), the network refused us (NoRouteToHost,
        # PermissionError, OSError), or icmplib couldn't open a socket.
        # In every case the user wants the dot to flip red, not stay
        # grey forever — once mDNS / MQTT / ping have all tried, the
        # signal is "we couldn't reach this device". A subsequent
        # successful ping will flip it right back to ONLINE.
        try:
            result = await icmp_ping(target, count=1, timeout=3, privileged=False)
            is_alive = result.is_alive
        except Exception as exc:
            # ``.local`` hosts on systems without Avahi / mdnsd hit
            # this every sweep; the traceback adds nothing and floods
            # the logs. One-line debug is plenty.
            _LOGGER.debug("Ping of %s (%s) failed: %s", device.name, target, exc)
            is_alive = False
        new_state = DeviceState.ONLINE if is_alive else DeviceState.OFFLINE
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
