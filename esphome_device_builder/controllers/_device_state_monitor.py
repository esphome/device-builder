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

try:
    from icmplib import async_ping as icmp_ping
except ImportError:  # pragma: no cover — icmplib is optional
    icmp_ping = None  # type: ignore[assignment]

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

# zeroconf hands us raw bytes for TXT keys; declared once so the
# call site can decode without re-typing the key.
_TXT_RECORD_VERSION = b"version"
_TXT_RECORD_CONFIG_HASH = b"config_hash"


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
        if self._mdns_browser is not None:
            try:
                await self._mdns_browser.async_cancel()
            except Exception:
                _LOGGER.debug("mDNS browser cancel failed", exc_info=True)
            self._mdns_browser = None
        if self._zeroconf is not None:
            try:
                await self._zeroconf.async_close()
            except Exception:
                _LOGGER.debug("zeroconf close failed", exc_info=True)
            self._zeroconf = None

    def priority_for(self, name: str) -> str:
        """Return the source currently authoritative for *name* (or "unknown")."""
        return self._state_source.get(name, "unknown")

    def apply(self, name: str, state: DeviceState, source: str) -> bool:
        """
        Record a state observation from *source*.

        Returns True when the observation actually changed the device's
        state and the change was forwarded to the callback. Sources
        below the current source's priority are ignored; same-state
        observations are no-ops.
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
        Return zeroconf-cached IPs for *host_name* without issuing a query.

        Returns ``None`` when zeroconf isn't running, the cache misses,
        or the entry has expired. mDNS-only — see
        :meth:`get_cached_dns_addresses` for non-``.local`` hostnames.
        """
        if self._zeroconf is None:
            return None
        try:
            from zeroconf import AddressResolver, IPVersion
        except ImportError:
            return None

        normalized = host_name.rstrip(".").lower()
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
            from zeroconf import ServiceStateChange
            from zeroconf.asyncio import AsyncServiceBrowser
        except ImportError:
            _LOGGER.warning("zeroconf not available — mDNS device discovery disabled")
            return

        try:
            self._zeroconf = AsyncEsphomeZeroconf()
        except Exception:
            _LOGGER.exception("Could not start zeroconf — falling back to ping only")
            self._zeroconf = None
            return

        loop = asyncio.get_running_loop()

        def _on_service_state_change(
            zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange
        ) -> None:
            # mDNS reports "<my-device>._esphomelib._tcp.local." — strip
            # the service suffix and convert hyphens (mDNS) back to
            # underscores (YAML config naming).
            device_name = name.split(".")[0].replace("-", "_")
            _LOGGER.debug("mDNS: %s %s (raw: %s)", state_change, device_name, name)

            # zeroconf callbacks fire on a different thread — bounce work
            # back to the asyncio loop. Added/Updated trigger an async
            # resolve so we can report the IP alongside the state change;
            # Removed clears state immediately.
            if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
                asyncio.run_coroutine_threadsafe(
                    self._resolve_and_apply(zeroconf, service_type, name, device_name),
                    loop,
                )
            elif state_change == ServiceStateChange.Removed:
                loop.call_soon_threadsafe(self.apply, device_name, DeviceState.OFFLINE, "mdns")
                loop.call_soon_threadsafe(self.apply_ip, device_name, "")
                self._state_source.pop(device_name, None)

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
        self, zeroconf: Any, service_type: str, name: str, device_name: str
    ) -> None:
        """Mark the device online and pull IP + firmware version from mDNS."""
        # State first — even if the resolve fails or times out, we know the device is online.
        self.apply(device_name, DeviceState.ONLINE, "mdns")

        try:
            from zeroconf import IPVersion
            from zeroconf.asyncio import AsyncServiceInfo
        except ImportError:
            return

        try:
            info = AsyncServiceInfo(service_type, name)
            if not await info.async_request(zeroconf, timeout=_MDNS_RESOLVE_TIMEOUT_MS):
                return
            addresses = info.parsed_scoped_addresses(
                IPVersion.V4Only
            ) or info.parsed_scoped_addresses(IPVersion.V6Only)
            if addresses:
                # Strip any zone suffix (e.g. "fe80::1%en0") for display purposes.
                ip = addresses[0].split("%", 1)[0]
                self.apply_ip(device_name, ip)
            properties = info.properties or {}
            version_bytes = properties.get(_TXT_RECORD_VERSION)
            if version_bytes:
                try:
                    self.apply_version(device_name, version_bytes.decode())
                except UnicodeDecodeError:
                    _LOGGER.debug(
                        "Could not decode mDNS version TXT for %s: %r",
                        device_name,
                        version_bytes,
                    )
            config_hash_bytes = properties.get(_TXT_RECORD_CONFIG_HASH)
            if config_hash_bytes:
                try:
                    self.apply_config_hash(device_name, config_hash_bytes.decode())
                except UnicodeDecodeError:
                    _LOGGER.debug(
                        "Could not decode mDNS config_hash TXT for %s: %r",
                        device_name,
                        config_hash_bytes,
                    )
        except Exception:
            _LOGGER.debug("mDNS resolve failed for %s", device_name, exc_info=True)

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

        # Skip devices already owned by a higher-priority source — pinging
        # them just to confirm what we already know wastes work and would
        # be ignored by ``apply()`` anyway.
        ping_priority = _SOURCE_PRIORITY["ping"]
        devices_to_ping = [
            d
            for d in self._get_devices()
            if d.address
            and _SOURCE_PRIORITY.get(self._state_source.get(d.name, "unknown"), 0) <= ping_priority
        ]
        if not devices_to_ping:
            return

        _LOGGER.debug("Pinging %d devices", len(devices_to_ping))

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
                    if not device.address.rstrip(".").lower().endswith(".local"):
                        self.apply_ip(device.name, target)
                ping_targets.append((device, target))
            await asyncio.gather(
                *(self._ping_device(device, target) for device, target in ping_targets),
                return_exceptions=True,
            )

    async def _ping_device(self, device: Device, target: str) -> None:
        try:
            result = await icmp_ping(target, count=1, timeout=3, privileged=False)
        except Exception:
            return
        new_state = DeviceState.ONLINE if result.is_alive else DeviceState.OFFLINE
        self.apply(device.name, new_state, "ping")
