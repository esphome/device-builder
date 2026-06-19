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

from ...helpers.subscriber_presence import SubscriberPresence
from ...models import AdoptableDevice, Device, DeviceState, ReachabilitySource
from .._reachability_tracker import MdnsCacheInfo, ReachabilityTracker
from .._task_controller_base import TaskControllerBase
from ._state import MonitorState
from .helpers import (
    _normalize_mac,
    _pick_ipv4,
)
from .importable import ImportableDiscovery
from .mdns import MdnsSource
from .ping import PingSource
from .shared import _SOURCE_PRIORITY

_LOGGER = logging.getLogger(__name__)
# Padding added to the cached A record's TTL when the drawer's
# refresh loop schedules its next probe. Sleeping ``ttl + this``
# guarantees ``async_resolve_host`` falls through its cache short-
# circuit and actually goes on the wire.
_MDNS_REFRESH_PADDING_SECONDS = 1.0


# Callback signature used by DeviceStateMonitor to push state changes
# back to its owner.
StateChangeCallback = Callable[[str, DeviceState, str], None]

# mDNS IP resolution callback. ``primary`` is the IPv4 we lock onto
# for ICMP / OTA cache args (or the first scoped IPv6 when no V4 is
# present); ``addresses`` is the announced set in zeroconf's
# ``parsed_scoped_addresses`` order. Empty primary + empty list
# signals the device went offline / was removed from mDNS.
IPChangeCallback = Callable[[str, str, list[str]], None]

# mDNS ``version`` TXT change.
VersionChangeCallback = Callable[[str, str], None]

# mDNS ``config_hash`` TXT change — 8-char lowercase hex of
# ``App.get_config_hash()``. Only broadcast by firmware built from
# esphome/esphome#16145 onwards; older devices never fire.
ConfigHashChangeCallback = Callable[[str, str], None]

# mDNS ``api_encryption`` TXT change — tri-state, see
# :meth:`DeviceStateMonitor.apply_api_encryption` for the empty-
# string-means-plaintext-confirmed contract.
ApiEncryptionChangeCallback = Callable[[str, str], None]

# mDNS ``mac`` TXT change. The value has already been normalised by
# :func:`_normalize_mac` to ``XX:XX:XX:XX:XX:XX`` so the frontend
# renders it directly. Empty / non-hex skips the callback so older
# firmware without the broadcast doesn't blank a known MAC.
MacAddressChangeCallback = Callable[[str, str], None]

# Discovery banner ADD / REMOVE — a device advertising
# ``package_import_url`` / ``project_name`` / ``project_version`` is
# a factory build ready to be adopted into the dashboard.
ImportableAddedCallback = Callable[[AdoptableDevice], None]
ImportableRemovedCallback = Callable[[str], None]


class DeviceStateMonitor(TaskControllerBase):  # noqa: PLR0904 (grandfathered; new public methods need a refactor first)
    """
    Drive device state from mDNS broadcasts plus periodic ICMP pings.

    Only one source can own a device's state at a time. mDNS always
    wins; ping only writes when mDNS hasn't already resolved the
    device. :meth:`priority_for` lets callers query the active source.
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
        super().__init__()
        self._get_devices = get_devices
        # ``get_devices_by_name`` is the scanner's O(1) name-keyed
        # index; mDNS / ping / MQTT key on ``esphome.name`` and fire
        # the apply path several times per broadcast, so a linear
        # scan of every YAML on every announcement is wrong at fleet
        # scale. Linear fallback kept for legacy tests that build a
        # monitor with just ``get_devices``.
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
        self.state = MonitorState(reachability=reachability)
        self._ping_task: asyncio.Task | None = None
        # ``self._tasks`` (fire-and-forget mDNS resolve refs) is
        # inherited from :class:`TaskControllerBase`.
        # When wired, the ping loop pauses while no dashboard client
        # is subscribed — closes a parity regression with the legacy
        # dashboard, which paused ICMP on an empty subscriber set.
        # ``None`` means "always run the loop" so existing tests
        # without a presence gate keep working.
        self._presence = presence
        self._importable = ImportableDiscovery(self)
        self._mdns = MdnsSource(self)
        self._ping = PingSource(self)

    async def start(self) -> None:
        """Start the mDNS browser and the periodic ping sweep."""
        await self._mdns.start()
        self._ping_task = asyncio.create_task(self._ping.run())

    async def stop(self) -> None:
        """Tear down the browser and cancel the ping loop."""
        if self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None
        # Cancel the browser FIRST so it stops dispatching new mDNS
        # callbacks; otherwise the drain below would race against
        # newly-spawned resolve tasks the browser is still firing.
        await self._mdns.cancel_browser()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        await self._mdns.close_zeroconf()

    @property
    def zeroconf(self) -> AsyncEsphomeZeroconf | None:
        """
        The mDNS responder powering device discovery, or ``None``.

        Exposed so the dashboard's own ``_esphomebuilder._tcp.local.``
        advertiser can reuse the same instance instead of opening a
        second responder. ``None`` when zeroconf failed to start
        (port held by avahi / ``mDNSResponder``).
        """
        return self._mdns.zeroconf

    def set_reachability(self, tracker: ReachabilityTracker) -> None:
        """
        Wire (or rewire) the per-signal freshness tracker.

        :class:`DevicesController` builds the monitor first so the
        tracker can take ``get_mdns_cache_info`` as its mDNS cache
        reader; this setter completes the wire-back.
        """
        self.state.reachability = tracker

    def priority_for(self, name: str) -> ReachabilitySource:
        """
        Return the source currently authoritative for *name*.

        Returns :data:`ReachabilitySource.UNKNOWN` when no source has
        claimed the device. Enum-typed so the drawer's reachability
        subscription can dispatch on it; the underlying ``StrEnum``
        means literal-string callers keep working unchanged.
        """
        return ReachabilitySource(self.state.state_source.get(name, ReachabilitySource.UNKNOWN))

    def forget(self, name: str) -> None:
        """Drop the source-precedence ledger entry for *name*."""
        self.state.state_source.pop(name, None)

    def apply(self, name: str, state: DeviceState, source: str, *, claim: bool = False) -> bool:
        """
        Record a state observation from *source*.

        Returns True iff the state was forwarded to the callback.
        A source below the current source's priority is ignored,
        except a positive ONLINE always revives a not-online device;
        observations where every matching device already carries
        *state* no-op.

        ``claim=True`` lets *source* take ownership even when the
        state is unchanged, blocking a lower-priority observation
        from later flipping the device back. The priority check
        governs OFFLINE/downgrades; a positive ONLINE from any
        source still revives a not-online device regardless of owner.
        """
        devices = self._get_devices_by_name(name)
        if not devices:
            _LOGGER.debug(
                "Device %s not in catalog — ignoring %s state from %s", name, state, source
            )
            return False

        # Record the per-signal observation regardless of whether
        # the priority check below ignores the new state — the user-
        # facing intent is "show every channel we're hearing on
        # independently", so a higher-priority source's claim
        # shouldn't hide that ping or MQTT also just answered. The
        # ONLINE filter avoids treating dropped-source OFFLINE flips
        # as freshness.
        if state == DeviceState.ONLINE and self.state.reachability is not None:
            self.state.reachability.observe(name, source)

        current_source = self.state.state_source.get(name, ReachabilitySource.UNKNOWN)
        # Scan *every* matching device, not just the first. Duplicate
        # ``esphome.name`` entries (a config plus a ``foo (1).yaml``
        # copy, dashboard_import siblings) share the broadcast — if one
        # sibling was rebuilt with state=UNKNOWN the first-match bail
        # would leave it stale.
        all_match = all(d.state == state for d in devices)
        # Online-wins: a positive reachability from any source brings a
        # not-online device online, even one a higher-priority source owns
        # (ping/MQTT reviving a device a stale mdns/mqtt OFFLINE owns). The
        # priority gate still governs OFFLINE/downgrades so a low-priority
        # flap can't drop a device a higher source confirmed online.
        online_takeover = state == DeviceState.ONLINE and not all_match
        if not online_takeover and _SOURCE_PRIORITY.get(source, 0) < _SOURCE_PRIORITY.get(
            current_source, 0
        ):
            return False
        if all_match:
            if claim:
                self.state.state_source[name] = source
            return False

        self.state.state_source[name] = source
        self._on_state_change(name, state, source)
        return True

    async def refresh_mdns(self, name: str) -> None:
        """Re-query a device's mDNS A/AAAA records via the wire."""
        await self._mdns.refresh_mdns(name)

    def get_mdns_a_record_ttl_remaining(self, name: str) -> float | None:
        """Return the minimum remaining TTL across cached A/AAAA records."""
        return self._mdns.get_mdns_a_record_ttl_remaining(name)

    def get_mdns_cache_info(self, name: str) -> MdnsCacheInfo | None:
        """Read the truthful "last heard via mDNS" age + remaining TTL."""
        return self._mdns.get_mdns_cache_info(name)

    def apply_ip(self, name: str, ip: str) -> bool:
        """
        Record a single-IP observation. Empty string clears stored IPs.

        Used by sources that only know one address per device
        (MQTT, DNS fallback). When *ip* is already in the device's
        ``ip_addresses`` list, only the primary slot is touched —
        a narrower MQTT / DNS observation must not shrink a multi-
        IP view mDNS already populated. Callers with the full
        announced set should reach for :meth:`apply_ip_addresses`.
        """
        if not ip:
            return self._dispatch_ip(name, "", [])
        devices = self._get_devices_by_name(name)
        if not devices:
            return False
        # Sample ``ip_addresses`` from the first match — duplicates
        # all flow through ``_dispatch_ip``'s fan-out so they
        # converge regardless of which we read here.
        existing = devices[0].ip_addresses
        addresses = list(existing) if ip in existing else [ip]
        return self._dispatch_ip(name, ip, addresses)

    def apply_ip_addresses(self, name: str, addresses: list[str]) -> bool:
        """
        Record the full set of announced IPs for *name*.

        Picks an IPv4 primary via :func:`_pick_ipv4` (falling back
        to the first scoped IPv6) so ``device.ip`` stays the single
        target for ICMP / OTA, and forwards the complete list to
        ``device.ip_addresses``. Empty list clears both.
        """
        primary = _pick_ipv4(addresses) if addresses else ""
        return self._dispatch_ip(name, primary, addresses)

    def _dispatch_ip(self, name: str, primary: str, addresses: list[str]) -> bool:
        """
        Shared dedupe + dispatch for both apply_ip variants.

        Dedupes against the configured devices' current ``ip`` and
        ``ip_addresses`` fields rather than a separate monitor
        cache so a Device rebuilt with ``previous=None`` (atomic-
        save REMOVE+re-ADD churn) gets repopulated on the next
        mDNS announcement. Either side differing fires — a host
        that picks up IPv6 while keeping its IPv4 still surfaces.
        """
        devices = self._get_devices_by_name(name)
        if not devices:
            return False
        if all(d.ip == primary and d.ip_addresses == addresses for d in devices):
            return False
        self._on_ip_change(name, primary, addresses)
        return True

    def apply_version(self, name: str, version: str) -> bool:
        """Record a firmware version observation; True iff forwarded."""
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
        confirms encryption is active. Empty string is the plaintext-
        confirmed signal — fired by ``MdnsSource._apply_service_info``
        in two wire shapes:

        1. TXT carries the ``api_encryption`` key with an empty /
           bare value (zeroconf collapses both to ``None``; apply
           normalises to ``""``).
        2. TXT carries other content (``version`` / ``mac`` / ...)
           but the ``api_encryption`` key is absent — ESPHome's
           atomic-per-announce TXT means the omission inside an
           otherwise-populated announce IS authoritative for
           "encryption was removed".

        ``MdnsSource._apply_service_info`` skips the empty-string
        apply when the announce is truly empty (cache eviction /
        fragment) — translating that to ``""`` would clobber the
        last-known truthy value and trip the "reinstall to apply"
        prompt.
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

        Empty strings dropped so pre-#16145 firmware (no
        ``config_hash`` TXT) doesn't churn the callback.
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

        Normalised via :func:`_normalize_mac` so the dedupe /
        sidecar / wire all stay canonical regardless of which case
        or separator style the firmware emits. Empty / non-hex
        inputs are dropped so a broadcast that omits the ``mac``
        TXT (older firmware) doesn't blank an already-known value.
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
        """
        Return True iff some configured device named *name* has ``attr != value``.

        Uses ``_get_devices_by_name``'s O(1) index so 1000-device
        fleets don't pay an O(N) scan on every mDNS broadcast.
        Short-circuits on the first stale match; False when no
        device matches (stray announcement) or every match already
        carries *value* (steady-state dedupe).
        """
        return any(getattr(device, attr) != value for device in self._get_devices_by_name(name))

    def get_cached_addresses(self, host_name: str) -> list[str] | None:
        """Return all zeroconf-cached IPs for *host_name* without issuing a query."""
        return self._mdns.get_cached_addresses(host_name)

    def probe_device(self, device_name: str, service_name: str | None = None) -> None:
        """Eagerly resolve a device's ``_esphomelib._tcp.local.`` service."""
        self._importable.probe_device(device_name, service_name)

    def probe_device_ping(self, device_name: str) -> None:
        """Wake the ICMP sweep loop; a herd of N adds collapses into one early sweep."""
        self._ping.wake()

    def revisit_importable(self, device_name: str) -> None:
        """Re-fire ``on_importable_added`` for *device_name* if upstream still has it cached."""
        self._importable.revisit_importable(device_name)

    def revisit_all_importables(self) -> None:
        """Re-fire ``on_importable_added`` for every cached importable."""
        self._importable.revisit_all_importables()

    def get_importable_devices(self) -> list[AdoptableDevice]:
        """Snapshot of devices currently advertising as importable."""
        return self._importable.get_importable_devices()

    def get_cached_dns_addresses(self, host_name: str) -> list[str] | None:
        """
        Return DNS-cached IPs for *host_name* without issuing a lookup.

        Populated by the ping sweep's pre-resolution pass; ``None``
        on cache miss or expired entry.
        """
        return self.state.dns_cache.get_cached_addresses(host_name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_device_by_name(self, name: str) -> Device | None:
        bucket = self._get_devices_by_name(name)
        return bucket[0] if bucket else None
