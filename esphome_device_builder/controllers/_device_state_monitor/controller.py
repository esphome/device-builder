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
A separate Native API fallback fills ``mac_address`` / ``deployed_version``
for online API devices mDNS hasn't reached; it never drives ONLINE/OFFLINE
and so stays out of the precedence ledger. The API reviver is the one
Native API path that does drive state: it revives a stuck-offline device
from its persisted IP after identity verification, claiming under the
``ping`` source.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ...helpers.async_ import create_eager_task, create_logged_task, drain_tasks
from ...helpers.ip import drop_unusable_addresses, is_unusable_address
from ...helpers.mac_addresses import normalize_mac
from ...helpers.subscriber_presence import SubscriberPresence
from ...models import (
    RUNTIME_STATE_FIELD_NAMES,
    AdoptableDevice,
    Device,
    DeviceState,
    ReachabilitySource,
)
from .._reachability_tracker import ReachabilityTracker
from .._task_controller_base import TaskControllerBase
from ._state import MonitorState
from .api_info import ApiInfoSource
from .api_reviver import ApiReviverSource
from .helpers import (
    _pick_ipv4,
)
from .importable import ImportableDiscovery
from .mdns import MdnsSource
from .ping import PingSource
from .shared import _SOURCE_PRIORITY, identity_source_owns, should_ping

_LOGGER = logging.getLogger(__name__)
# Cap on draining the ping / API-info / resolve tasks at shutdown.
_STOP_DRAIN_TIMEOUT = 2.0


# Callback signature used by DeviceStateMonitor to push state changes
# back to its owner.
StateChangeCallback = Callable[[str, DeviceState, str], None]

# Authoritative-source change: fired when the winning ``priority_for``
# source for a device flips (e.g. mDNS goes dark and ping takes over, or
# mDNS claims a ping-only device). Lets the owner surface ``active_source``
# on the device snapshot so the UI can gate mDNS-sourced indicators.
SourceChangeCallback = Callable[[str, ReachabilitySource], None]

# mDNS IP resolution callback. ``primary`` is the IPv4 we lock onto
# for ICMP / OTA cache args (or the first scoped IPv6 when no V4 is
# present); ``addresses`` is the announced set in zeroconf's
# ``parsed_scoped_addresses`` order. Both are always non-empty — a
# confirmed loss of resolution flows through
# ``ResolvedAddressesClearedCallback`` instead.
IPChangeCallback = Callable[[str, str, list[str]], None]

# Confirmed loss of mDNS resolution for *name*: drop the resolved
# ``ip_addresses`` set. ``device.ip`` keeps the last-known primary
# (RAM mirrors the metadata sidecar) so the api_reviver can repair a
# mid-process mDNS death; only its identity-verified invalidation
# clears that value.
ResolvedAddressesClearedCallback = Callable[[str], None]

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
# :func:`normalize_mac` to ``XX:XX:XX:XX:XX:XX`` so the frontend
# renders it directly. Empty / non-hex skips the callback so older
# firmware without the broadcast doesn't blank a known MAC.
MacAddressChangeCallback = Callable[[str, str], None]


# Deployed-identity freshness: True when first-party evidence was just
# applied — an identity-carrying ``_http._tcp`` TXT (non-API, live or
# from the unexpired cache), a Native API ``device_info`` connection
# (api devices the mDNS ledger doesn't own), or our own flash. False
# after a targeted ``_http._tcp`` re-resolve confirmed the TXT gone, or
# when mDNS takes ownership of an api device (its announce lifecycle
# vouches from then on). Never a reachability claim. A Protocol (not a
# ``Callable`` alias) because ``live`` is keyword-only at every
# implementer.
class DeployedIdentityLiveCallback(Protocol):
    def __call__(self, name: str, *, live: bool) -> None: ...


# Discovery banner ADD / REMOVE — a device advertising
# ``package_import_url`` / ``project_name`` / ``project_version`` is
# a factory build ready to be adopted into the dashboard.
ImportableAddedCallback = Callable[[AdoptableDevice], None]
ImportableRemovedCallback = Callable[[str], None]

# Native API connection resolver keyed on the YAML filename, returning
# ``(encryption_key, port)``. The API info fallback uses it to reach a
# device; an empty key means "connect plaintext".
ApiConnectionResolver = Callable[[str], Awaitable[tuple[str, int]]]

# Persisted-IP invalidation, ``(name, stale_ip)``: the API reviver
# proved the on-disk last-known *stale_ip* now belongs to a different
# device, so the owner must clear it (``on_ip_change`` deliberately
# keeps last-known on disk). Carrying the proven IP scopes the clear —
# a same-name sibling holding a different IP, or an IP mDNS re-learned
# mid-dial, stays untouched.
PersistedIpInvalidatedCallback = Callable[[str, str], None]


class DeviceStateMonitor(TaskControllerBase):
    """
    Drive device state from mDNS broadcasts plus periodic ICMP pings.

    Only one source can own a device's state at a time. mDNS always
    wins; ping only writes when mDNS hasn't already resolved the
    device. :meth:`priority_for` lets callers query the active source.

    The public source attributes (``mdns``, ``ping``, ``importable``,
    ``api_info``, ``api_reviver``) are the sanctioned seams for
    per-source queries and nudges; the monitor keeps only the
    cross-source core (lifecycle, the precedence ledger, ``apply_*``).
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
        resolve_api_connection: ApiConnectionResolver | None = None,
        on_source_change: SourceChangeCallback | None = None,
        on_persisted_ip_invalidated: PersistedIpInvalidatedCallback | None = None,
        on_resolved_addresses_cleared: ResolvedAddressesClearedCallback | None = None,
        on_deployed_identity_live_change: DeployedIdentityLiveCallback | None = None,
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
        self._on_source_change = on_source_change
        self._on_version_change = on_version_change
        self._on_config_hash_change = on_config_hash_change
        self._on_api_encryption_change = on_api_encryption_change
        self._on_mac_address_change = on_mac_address_change
        self._on_importable_added = on_importable_added
        self._on_importable_removed = on_importable_removed
        self._is_ignored = is_ignored or (lambda _name: False)
        self._resolve_api_connection = resolve_api_connection
        self._on_persisted_ip_invalidated = on_persisted_ip_invalidated
        self._on_resolved_addresses_cleared = on_resolved_addresses_cleared
        self._on_deployed_identity_live_change = on_deployed_identity_live_change
        self.state = MonitorState(reachability=reachability)
        self._ping_task: asyncio.Task | None = None
        self._api_info_task: asyncio.Task | None = None
        self._api_reviver_task: asyncio.Task | None = None
        # ``self._tasks`` (fire-and-forget mDNS resolve refs) is
        # inherited from :class:`TaskControllerBase`.
        # When wired, the ping loop pauses while no dashboard client
        # is subscribed — closes a parity regression with the legacy
        # dashboard, which paused ICMP on an empty subscriber set.
        # ``None`` means "always run the loop" so existing tests
        # without a presence gate keep working.
        self.presence = presence
        self.importable = ImportableDiscovery(self)
        # One Native API worker dial at a time across every API source
        # (api_info + reviver): each dial spawns an interpreter and
        # occupies one of a device's scarce API connection slots.
        self._api_dial_budget = asyncio.Semaphore(1)
        self.mdns = MdnsSource(self)
        self.ping = PingSource(self)
        self.api_info = ApiInfoSource(self)
        self.api_reviver = ApiReviverSource(self)

    async def start(self) -> None:
        """Start the importable flow, mDNS browser, ping sweep, API info fallback, and reviver."""
        self.importable.setup()
        await self.mdns.start()
        self._ping_task = create_logged_task(self.ping.run(), name="Ping sweep")
        self._api_info_task = create_logged_task(self.api_info.run(), name="API info fallback")
        self._api_reviver_task = create_logged_task(self.api_reviver.run(), name="API reviver")

    async def stop(self) -> None:
        """Tear down the browser and drain the ping + API + resolve tasks (bounded)."""
        drain: list[asyncio.Task[Any]] = []
        # Cancel the loop tasks eagerly — before the ``cancel_browser``
        # await below — so a mid-sweep loop can't resume and spawn new
        # work (a 15s worker subprocess, state applies) during teardown.
        # ``drain_tasks`` re-cancelling later is a no-op.
        for attr in ("_ping_task", "_api_info_task", "_api_reviver_task"):
            task: asyncio.Task | None = getattr(self, attr)
            if task is not None:
                task.cancel()
                drain.append(task)
                setattr(self, attr, None)
        # Cancel the browser FIRST so it stops dispatching new mDNS
        # callbacks; otherwise the drain below would race against
        # newly-spawned resolve tasks the browser is still firing.
        await self.mdns.cancel_browser()
        drain.extend(self._tasks)
        self._tasks.clear()
        # Close zeroconf eagerly so 5353 frees now, overlapping the task drain.
        close = create_eager_task(self.mdns.close_zeroconf())
        if drain:
            # Drain bounded here so the cancelled tasks don't leak to aiohttp's
            # unbounded terminal sweep.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(drain_tasks(drain), _STOP_DRAIN_TIMEOUT)
        await close

    def set_reachability(self, tracker: ReachabilityTracker) -> None:
        """
        Wire (or rewire) the per-signal freshness tracker.

        :class:`DevicesController` builds the monitor first so the
        tracker can take ``mdns.get_mdns_cache_info`` as its mDNS
        cache reader; this setter completes the wire-back.
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
        if (old := self._pop_source(name)) is not None:
            self._emit_source_change(name, old, ReachabilitySource.UNKNOWN)

    def source_withdrawn(self, name: str, source: str) -> None:
        """
        Release every per-name claim after *source* withdrew.

        A no-op unless *source* currently owns the ledger — a source
        releases only a claim it holds. An already-OFFLINE bucket
        keeps its state; the ledger is never stamped, so the
        withdrawal emits a single source change. With no ICMP arbiter
        the withdrawal demotes to OFFLINE instead of parking on
        UNKNOWN. An api bucket with known identity gets
        ``deployed_identity_live`` stamped back — the announce
        lifecycle stops vouching the moment ownership is released.
        """
        # A late Removed for a name another source owns adds nothing,
        # and popping an MQTT-owned claim would flap a device the
        # broker still vouches for.
        if self.priority_for(name) is not ReachabilitySource(source):
            return
        # UNKNOWN only under a confirmed arbiter: a pre-probe (None)
        # withdrawal on a host whose probe then fails would park
        # UNKNOWN with no sweep ever running. Demoting instead always
        # self-corrects — the sweep pings OFFLINE devices every pass.
        state = DeviceState.UNKNOWN if self.ping.icmp_available is True else DeviceState.OFFLINE
        already_offline = not self._any_matching_device_differs(name, "state", DeviceState.OFFLINE)
        if not already_offline and self._any_matching_device_differs(name, "state", state):
            self._on_state_change(name, state, source)
        self.clear_resolved_addresses(name)
        # The identity hand-back stamps between the ledger pop and its
        # notification: the stamp is refused while mDNS owns the name,
        # and stamping before the source change keeps every frame
        # holding the frontend gate through one disjunct or the other
        # (the clear-side ordering rule in ``_emit_source_change``).
        old = self._pop_source(name)
        if self._has_known_api_identity(name):
            self.apply_deployed_identity_live(name, live=True)
        if old is not None:
            self._emit_source_change(name, old, ReachabilitySource.UNKNOWN)

    def _emit_source_change(self, name: str, old: str, new: str) -> None:
        """Notify the owner when *name*'s authoritative source actually flips."""
        if old == new:
            return
        if self._on_source_change is not None:
            self._on_source_change(name, ReachabilitySource(new))
        # Callers update the ledger before emitting, so the predicate is
        # true exactly on transitions INTO api-device mDNS ownership —
        # the moment the announce lifecycle takes over vouching for the
        # identity (see _mdns_owns_api_identity). Cleared after the
        # source notification so no DEVICE_UPDATED frame ever shows the
        # flag down before active_source says mdns — every frame holds
        # the frontend gate through one disjunct or the other.
        if self._mdns_owns_api_identity(name):
            self.apply_deployed_identity_live(name, live=False)

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
        all_match = all(d.runtime_state.state == state for d in devices)
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
                self._emit_source_change(name, current_source, source)
            return False

        self.state.state_source[name] = source
        self._emit_source_change(name, current_source, source)
        self._on_state_change(name, state, source)
        return True

    def apply_ip(self, name: str, ip: str) -> bool:
        """
        Record a single-IP observation; *ip* must be truthy.

        Used by sources that only know one address per device
        (MQTT, DNS fallback). When *ip* is already in the device's
        ``ip_addresses`` list, only the primary slot is touched —
        a narrower MQTT / DNS observation must not shrink a multi-
        IP view mDNS already populated. Callers with the full
        announced set should reach for :meth:`apply_ip_addresses`;
        a confirmed loss of resolution goes through
        :meth:`clear_resolved_addresses`.
        """
        if not ip:
            raise ValueError("empty ip; use clear_resolved_addresses")
        if is_unusable_address(ip):
            return False
        devices = self._get_devices_by_name(name)
        if not devices:
            return False
        # Sample ``ip_addresses`` from the first match — duplicates
        # all flow through ``_dispatch_ip``'s fan-out so they
        # converge regardless of which we read here.
        existing = devices[0].runtime_state.ip_addresses
        addresses = list(existing) if ip in existing else [ip]
        return self._dispatch_ip(name, ip, addresses)

    def apply_ip_addresses(self, name: str, addresses: list[str]) -> bool:
        """
        Record the full set of announced IPs; *addresses* must be non-empty.

        Picks an IPv4 primary via :func:`_pick_ipv4` (falling back
        to the first scoped IPv6) so ``device.ip`` stays the single
        target for ICMP / OTA, and forwards the complete list to
        ``runtime_state.ip_addresses``.
        """
        if not addresses:
            raise ValueError("empty addresses; use clear_resolved_addresses")
        usable = drop_unusable_addresses(addresses)
        if not usable:
            return False
        return self._dispatch_ip(name, _pick_ipv4(usable), usable)

    def clear_resolved_addresses(self, name: str) -> bool:
        """
        Drop the resolved-address set after a confirmed loss of mDNS.

        ``device.ip`` keeps the last-known primary (RAM mirrors the
        metadata sidecar); only the api_reviver's identity-verified
        invalidation clears that value.
        """
        if self._on_resolved_addresses_cleared is None:
            return False
        devices = self._get_devices_by_name(name)
        if not devices or all(not d.runtime_state.ip_addresses for d in devices):
            return False
        self._on_resolved_addresses_cleared(name)
        return True

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
        if all(d.ip == primary and d.runtime_state.ip_addresses == addresses for d in devices):
            return False
        self._on_ip_change(name, primary, addresses)
        return True

    def apply_version(self, name: str, version: str) -> bool:
        """Record a firmware version observation; True iff forwarded."""
        if not version or (forward := self._on_version_change) is None:
            return False
        return self._apply_observation(name, "deployed_version", version, forward, name, version)

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
        if (forward := self._on_api_encryption_change) is None:
            return False
        return self._apply_observation(
            name, "api_encryption_active", encryption, forward, name, encryption
        )

    def apply_config_hash(self, name: str, config_hash: str) -> bool:
        """
        Record a running-firmware config hash observation.

        Empty strings dropped so pre-#16145 firmware (no
        ``config_hash`` TXT) doesn't churn the callback.
        """
        if not config_hash or (forward := self._on_config_hash_change) is None:
            return False
        return self._apply_observation(
            name, "deployed_config_hash", config_hash, forward, name, config_hash
        )

    def apply_mac_address(self, name: str, mac: str) -> bool:
        """
        Record a MAC-address observation from the device's mDNS TXT.

        Normalised via :func:`normalize_mac` so the dedupe /
        sidecar / wire all stay canonical regardless of which case
        or separator style the firmware emits. Empty / non-hex
        inputs are dropped so a broadcast that omits the ``mac``
        TXT (older firmware) doesn't blank an already-known value.
        """
        if (forward := self._on_mac_address_change) is None:
            return False
        normalized = normalize_mac(mac)
        if not normalized:
            return False
        return self._apply_observation(name, "mac_address", normalized, forward, name, normalized)

    def apply_deployed_identity_live(self, name: str, *, live: bool) -> bool:
        """
        Record whether fresh first-party evidence backs *name*'s identity; True iff forwarded.

        A ``live=True`` stamp is refused while mDNS owns an api device,
        so every evidence source (Native API dial, post-flash sync)
        stamps unconditionally without racing a mid-probe mdns claim —
        a stamp made under ownership would never see the
        transition-into-mdns clear in :meth:`_emit_source_change`.
        """
        if (forward := self._on_deployed_identity_live_change) is None:
            return False
        if live and self._mdns_owns_api_identity(name):
            return False
        return self._apply_observation(
            name, "deployed_identity_live", live, forward, name, live=live
        )

    def _pop_source(self, name: str) -> str | None:
        """Remove and return *name*'s ledger entry without notifying."""
        return self.state.state_source.pop(name, None)

    def _has_known_api_identity(self, name: str) -> bool:
        """
        Whether some api device named *name* already carries identity fields.

        Version / mac only — the fields a Native API ``device_info``
        can vouch for. ``deployed_config_hash`` is mDNS-TXT-born and
        never arrives without ``version`` beside it.
        """
        return any(
            d.api_enabled and (d.runtime_state.deployed_version or d.mac_address)
            for d in self._get_devices_by_name(name)
        )

    def _mdns_owns_api_identity(self, name: str) -> bool:
        """
        Report whether mDNS owns *name* while the bucket has an api device.

        The one condition under which ``deployed_identity_live`` must
        stay down: the announce lifecycle vouches for an api device's
        identity while mDNS owns it, so an in-flight stamp can't race
        the transition-into-mdns clear; the withdrawal owns the
        hand-back. Deliberately false for non-api
        buckets — their mdns ownership is a bare A-record resolve,
        reachability only, and suppressing their stamps would strand a
        post-flash stamp on firmware with no identity TXT to re-stamp
        it.
        """
        return self.priority_for(name) is ReachabilitySource.MDNS and any(
            device.api_enabled for device in self._get_devices_by_name(name)
        )

    def _apply_observation[**P](
        self,
        name: str,
        attr: str,
        value: Any,
        forward: Callable[P, None],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> bool:
        """Differ-gate an observation; invoke *forward* and return True iff it changed anything.

        Takes the callback and its arguments separately so the
        steady-state dedupe path allocates no wrapper.
        """
        if not self._any_matching_device_differs(name, attr, value):
            return False
        forward(*args, **kwargs)
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
        on_runtime = attr in RUNTIME_STATE_FIELD_NAMES
        return any(
            getattr(device.runtime_state if on_runtime else device, attr) != value
            for device in self._get_devices_by_name(name)
        )

    def invalidate_persisted_ip(self, name: str, stale_ip: str) -> None:
        """Tell the owner *name*'s persisted *stale_ip* is proven stale; no-op when unwired."""
        if self._on_persisted_ip_invalidated is not None:
            self._on_persisted_ip_invalidated(name, stale_ip)

    def address_retargeted(self, name: str) -> None:
        """Drop stale resolved IPs and wake the sweep after *name*'s configured address changed."""
        # mDNS / MQTT evidence is identity-carrying and address-
        # independent, so their resolved set survives the retarget.
        if not identity_source_owns(self, name):
            self.clear_resolved_addresses(name)
        self.probe_device_ping(name)

    def probe_device_ping(self, device_name: str) -> None:
        """
        Wake the ICMP sweep loop when *device_name* warrants a probe.

        Skipped when :func:`should_ping` says every matching device is
        already ONLINE under a higher-priority source (the sweep would
        filter it out anyway); unknown names fail open. A herd of N
        calls collapses into one early sweep.
        """
        devices = self._get_devices_by_name(device_name)
        if not devices or any(should_ping(self, d) for d in devices):
            self.ping.wake()

    def probe_reachability(self, device_name: str) -> None:
        """Full reachability nudge: eager mDNS resolve + ICMP sweep wake for *device_name*."""
        self.mdns.probe_device(device_name)
        self.probe_device_ping(device_name)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_device_by_name(self, name: str) -> Device | None:
        bucket = self._get_devices_by_name(name)
        return bucket[0] if bucket else None
