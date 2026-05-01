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

from esphome.zeroconf import AsyncEsphomeZeroconf
from zeroconf import AddressResolver, IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

try:
    from icmplib import async_ping as icmp_ping
except ImportError:  # pragma: no cover — icmplib is optional
    icmp_ping = None  # type: ignore[assignment]

from ..helpers.hostname import is_local_hostname, normalize_hostname
from ..models import Device, DeviceState
from ._dns_cache import DNSCache

_LOGGER = logging.getLogger(__name__)
_ESPHOME_SERVICE_TYPE = "_esphomelib._tcp.local."
_PING_INTERVAL = 60  # seconds between ping sweeps
_PING_BATCH_SIZE = 10
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
    ) -> None:
        self._get_devices = get_devices
        self._on_state_change = on_state_change
        self._on_ip_change = on_ip_change
        self._on_version_change = on_version_change
        self._on_config_hash_change = on_config_hash_change
        self._state_source: dict[str, str] = {}  # device name → "mdns" | "ping"
        self._device_ips: dict[str, str] = {}  # device name → last known IP
        self._device_versions: dict[str, str] = {}  # device name → last reported version
        self._device_config_hashes: dict[str, str] = {}  # device name → last reported config hash
        self._zeroconf: AsyncEsphomeZeroconf | None = None
        self._mdns_browser: Any = None
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

        def _on_service_state_change(
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

        try:
            self._mdns_browser = AsyncServiceBrowser(
                self._zeroconf.zeroconf,
                _ESPHOME_SERVICE_TYPE,
                handlers=[_on_service_state_change],
            )
            _LOGGER.info("mDNS browser started for %s", _ESPHOME_SERVICE_TYPE)
        except Exception:
            _LOGGER.exception("Could not start mDNS browser — device discovery limited to ping")

    async def _resolve_and_apply(
        self, zeroconf: Any, info: AsyncServiceInfo, device_name: str
    ) -> None:
        """Resolve a cache-miss mDNS service and propagate its details."""
        try:
            if not await info.async_request(zeroconf, timeout=_MDNS_RESOLVE_TIMEOUT_MS):
                return
        except Exception:
            _LOGGER.debug("mDNS resolve failed for %s", device_name, exc_info=True)
            return
        self._apply_service_info(device_name, info)

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
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL)
                await self._ping_sweep()
        except asyncio.CancelledError:
            pass

    async def _ping_sweep(self) -> None:
        if icmp_ping is None:
            return

        # Match the upstream dashboard: only ping devices that aren't
        # already ONLINE from a higher-priority source. ``OFFLINE`` and
        # ``UNKNOWN`` devices still get pinged so off-network hosts (no
        # mDNS reachability) can transition online via DNS + ICMP.
        devices_to_ping: list[Device] = []
        for device in self._get_devices():
            if not device.address or not self._should_ping(device):
                continue
            # Zeroconf's cache is authoritative for ``.local`` — if it
            # has an entry, the device announced via mDNS even when the
            # ``AsyncServiceBrowser`` ``Added`` callback didn't fire for
            # us (multicast packet drops, startup race). Claim it as
            # mDNS-online and skip ping; otherwise the bare-hostname DNS
            # fallback can resolve to an unreachable IP on a different
            # subnet and we'd report a phantom OFFLINE for a device
            # that's actually right there.
            if is_local_hostname(device.address) and (
                cached := self.get_cached_addresses(device.address)
            ):
                self.apply(device.name, DeviceState.ONLINE, "mdns", claim=True)
                # ``apply_ip`` only carries one IP; prefer V4 so the
                # device-list display and any ad-hoc ICMP probe both get
                # the cross-subnet-friendly entry. The CLI cache args
                # built in ``_build_address_cache_args`` consume every
                # cached IP separately, so we don't lose V6 reachability
                # by picking V4 here.
                self.apply_ip(device.name, _pick_ipv4(cached))
                continue
            devices_to_ping.append(device)
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
            # otherwise re-resolve internally on every ping, and the
            # OTA cache args would have nothing to draw on for non-mDNS
            # hostnames.
            resolved = await asyncio.gather(
                *(self._dns_cache.async_resolve(d.address) for d in batch),
                return_exceptions=True,
            )
            ping_targets: list[tuple[Device, str]] = []
            for device, addresses in zip(batch, resolved, strict=True):
                target = device.address
                if isinstance(addresses, list) and addresses:
                    target = addresses[0]
                    # mDNS owns IP tracking for ``.local`` hosts; only
                    # backfill from DNS for non-mDNS hosts so a stale
                    # DNS result can't clobber the live mDNS value.
                    if not is_local_hostname(device.address):
                        self.apply_ip(device.name, target)
                ping_targets.append((device, target))
            await asyncio.gather(
                *(self._ping_device(device, target) for device, target in ping_targets),
                return_exceptions=True,
            )

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
        try:
            result = await icmp_ping(target, count=1, timeout=3, privileged=False)
        except Exception:
            return
        new_state = DeviceState.ONLINE if result.is_alive else DeviceState.OFFLINE
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
