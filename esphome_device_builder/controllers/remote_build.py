"""
Remote-build feature; peer dashboard discovery + pairing + peers.

Browses ``_esphomebuilder._tcp.local.`` to list other dashboards
reachable on the LAN; persists the receiver-side ``enabled``
master switch, the user-supplied manual-host list for
cross-subnet / non-multicast LANs, and the paired-peer list;
merges discovery sources into a single
``remote_build/list_hosts`` snapshot.

The ``enabled`` flag gates the peer-link Noise WS listener
:class:`DeviceBuilder` binds at startup
(``/remote-build/peer-link``, default port 6055). Toggling
``enabled`` at runtime persists the new value but does NOT
live-bind / unbind the listener; flipping it requires a
dashboard restart for the listener state to follow. The 3c
Settings UI surfaces this constraint; a future PR can wire
the start / stop hooks if interactive toggling matters.

Pairing model (phase 4a-r1):

* Receiver-side state is a list of :class:`StoredPeer` rows
  keyed on ``dashboard_id``, with X25519 ``pin_sha256`` +
  ``static_x25519_pub`` derived from the offloader's peer-link
  Noise handshake transcript.
* Approval is a two-step gate: the offloader's first
  ``pair_request`` lands a ``PENDING`` row inside the
  receiver-controlled "pairing window"; the receiver UI
  shows the row in the inbox and the user clicks
  Accept, which calls the ``remote_build/approve_peer`` WS
  command → :meth:`RemoteBuildController.approve_peer` (the
  per-row counterpart to :meth:`record_pair_request`).
* Approved peers can then run ``intent="peer_link"`` against
  the same ``/remote-build/peer-link`` endpoint without
  re-prompting the receiver-side user.

The HTTPS+bearer receiver site that shipped in phases 3b1-3c
(token CRUD, ``StoredToken`` persistence, bearer auth
middleware, first-use binding) was wound down across phases
4a-r1 (listener body swap to Noise WS) and 4a-r2 (helper
deletion); only ``StoredPeer`` + the peer-link Noise dispatch
ship in production today. See issue #106 for the historical
trail.

Manual hosts have no version / fingerprint resolution yet;
they land in ``list_hosts`` with empty ``server_version`` /
``esphome_version`` until pairing attempts the connection.

Browser uses the existing ``AsyncEsphomeZeroconf`` instance owned by
:class:`~esphome_device_builder.controllers._device_state_monitor.DeviceStateMonitor`,
so the dashboard ships one mDNS responder per process and this
controller adds a second :class:`~zeroconf.asyncio.AsyncServiceBrowser`
on the same instance for the new service type. The state monitor's
own browsers (``_esphomelib._tcp.local.`` for devices,
``_http._tcp.local.`` for adoptable web UIs) are unaffected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass as _dataclass
from typing import TYPE_CHECKING, Any, Literal

from esphome.const import __version__ as esphome_version
from yarl import URL
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from ..constants import __version__ as server_version
from ..helpers.api import CommandError, api_command
from ..helpers.dashboard_advertise import SERVICE_TYPE
from ..helpers.dashboard_identity import (
    DASHBOARD_ID_MAX_CHARS,
    DASHBOARD_ID_PATTERN,
    get_or_create_identity,
    rotate_certificate,
)
from ..helpers.peer_link_identity import get_or_create_peer_link_identity
from ..models import (
    ErrorCode,
    EventType,
    IdentityView,
    IntentResponse,
    ManualHost,
    PairingWindowState,
    PeerStatus,
    PeerSummary,
    RemoteBuildPairingWindowChangedData,
    RemoteBuildPairRequestReceivedData,
    RemoteBuildPairStatusChangedData,
    RemoteBuildPeer,
    RemoteBuildPeerSource,
    RemoteBuildSettings,
    RemoteBuildSettingsView,
    StoredPeer,
)
from .config import load_remote_build_settings, remote_build_settings_transaction
from .remote_build_peer_link_client import (
    PeerLinkClientError,
)
from .remote_build_peer_link_client import (
    preview_pair as peer_link_preview_pair,
)

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder
    from ..helpers.dashboard_identity import DashboardIdentity

_LOGGER = logging.getLogger(__name__)

# Timeout for the cache-miss resolve path. Longer than
# ``DeviceStateMonitor._MDNS_RESOLVE_TIMEOUT_MS`` (2s) because peer
# dashboards typically run on full hosts (laptop, desktop, addon
# container) that may be a few hops further away on the LAN than
# an ESPHome device, and the user-visible cost of a slow first
# discovery is "the peer doesn't appear in Settings for a few
# seconds"; not the device-state miss the shorter timeout
# protects against.
_RESOLVE_TIMEOUT_MS = 3000

# Default lifetime of a pairing window (seconds). The window opens
# when the receiver-side Pairing requests screen mounts and
# auto-closes after this much idle time. The frontend extends by
# calling ``remote_build/set_pairing_window`` with ``open=true``
# again on each user-activity tick (debounced to once per 30s on
# the wire). Five minutes balances "long enough to OOB-confirm a
# pin without rushing" against "short enough that an idle tab
# isn't an attack surface". See issue #106 design choice (c).
_PAIRING_WINDOW_DURATION_SECONDS = 300.0


@_dataclass
class _PairRequestOutcome:
    """
    Out-param for ``record_pair_request``'s settings mutator.

    The mutator runs inside a sync transaction (``_modify_settings``
    drives it on the disk-write hop) and needs to communicate back
    to the async caller whether the row was created / refreshed /
    already-APPROVED / pin-mismatched. A dataclass beats a
    ``nonlocal`` because the data flow is explicit at the call
    site — a reader can grep for ``_PairRequestOutcome`` and find
    the contract — and future fields (an event payload, metrics)
    can be added without nonlocal-ing each new variable.
    """

    response: IntentResponse | None = None


def _decode_txt_value(raw: bytes | None) -> str:
    """Decode a TXT value as UTF-8, falling back to the empty string."""
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _peer_from_service_info(name: str, info: AsyncServiceInfo) -> RemoteBuildPeer:
    """
    Build a :class:`RemoteBuildPeer` from a resolved ``AsyncServiceInfo``.

    Keeps the parsing in one place so ``_apply_service_info`` and
    the cache-hit branch produce identical shapes.

    Uses ``parsed_scoped_addresses(IPVersion.All)`` rather than
    ``parsed_addresses()`` so IPv6 link-local entries keep their
    ``%<interface>`` scope suffix. Without the scope, an
    ``fe80::xxx`` address parses but isn't connectable; the OS
    needs to know which interface to send the packet out on.
    Mirrors the choice already made in
    :class:`DeviceStateMonitor` (line 901).
    """
    properties = info.properties or {}
    server_version = _decode_txt_value(properties.get(b"server_version"))
    esphome_version = _decode_txt_value(properties.get(b"esphome_version"))
    # ``info.name`` comes back as ``<instance>.<service_type>``; we
    # only want the leftmost label as the friendly name.
    instance = (info.name or name).split(".", 1)[0]
    server = info.server or ""
    return RemoteBuildPeer(
        name=instance,
        hostname=server,
        port=info.port or 0,
        source=RemoteBuildPeerSource.MDNS,
        addresses=info.parsed_scoped_addresses(IPVersion.All) or [],
        server_version=server_version,
        esphome_version=esphome_version,
    )


def _peer_from_manual_host(entry: ManualHost) -> RemoteBuildPeer:
    """
    Build a :class:`RemoteBuildPeer` from a stored :class:`ManualHost`.

    Manual hosts skip resolution; phase 2b just surfaces the
    user-entered ``(hostname, port)`` so the frontend can render
    the row alongside mDNS-discovered ones. Phase 4 attempts the
    actual connection and fills the version fields.

    ``name`` is the hostname verbatim (rather than the leftmost
    label) so an IP-only entry still reads sensibly in the UI;
    the frontend can render a "Manual" badge to distinguish it
    from an mDNS-discovered row.
    """
    return RemoteBuildPeer(
        name=entry.hostname,
        hostname=entry.hostname,
        port=entry.port,
        source=RemoteBuildPeerSource.MANUAL,
    )


_HOSTNAME_MAX_CHARS = 255  # RFC 1035 §2.3.4 caps a FQDN at 253; round up to 255.


def _validate_hostname(raw: object) -> str:
    """
    Normalise a user-entered hostname to its canonical lowercase form.

    Rejects non-string and empty / whitespace-only input with
    :class:`CommandError(INVALID_ARGS)`. Caps length at
    :data:`_HOSTNAME_MAX_CHARS` (RFC 1035 §2.3.4 caps a fully-
    qualified domain name at 253 characters; we accept up to 255
    to leave room for trailing-dot variations). The cap stops a
    misbehaving frontend from bloating the on-disk sidecar (and,
    for the offloader-side pairing pool, the wire payload of
    ``list_pool``) with a megabyte-string masquerading as a
    hostname.

    Defers the URL-validity check to :class:`yarl.URL.build` so
    the WS-command validator and the offloader's
    ``_build_ws_url`` (in
    :mod:`controllers.remote_build_peer_link_client`) share a
    single source of truth on what constitutes a host. yarl
    rejects ``/``, ``?``, ``#``, ``@``, embedded ``:port``, and
    other URL-injection shapes that would otherwise let a
    pathological hostname smuggle path / query / fragment /
    userinfo into the rendered URL. Without this layered check
    the offloader's ``preview_pair`` would have to catch the
    ``ValueError`` from ``URL.build`` at request time and map
    it to ``UNAVAILABLE``; surfacing the same shape as
    ``INVALID_ARGS`` here means the frontend gets a "fix your
    input" diagnostic rather than a "transient remote
    failure" toast.

    Lowercase normalisation matches the duplicate-check
    semantics; hostnames are case-insensitive per RFC 1035 §2.3.3,
    so ``Desktop.local`` and ``desktop.local`` should be the same
    entry. The stored form is the trimmed, lowercased string (so
    two adds with different casing collapse to one entry rather
    than registering twice). Phase 4 attempts the actual
    connection (and discovers DNS / TLS validity); phase 2b
    deliberately doesn't pre-flight an "is this resolvable now?"
    check, which would fail on offline laptops adding a peer
    for later.
    """
    if not isinstance(raw, str):
        msg = "manual host: 'hostname' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip().lower()
    if not trimmed:
        msg = "manual host: 'hostname' must not be empty"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if len(trimmed) > _HOSTNAME_MAX_CHARS:
        msg = f"manual host: 'hostname' must be at most {_HOSTNAME_MAX_CHARS} characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    # The ``port=80, path="/"`` are sentinels for the build call
    # — only the host arg is being validated. yarl's host parser
    # is the same one ``_build_ws_url`` will use later, so any
    # input that passes here is guaranteed to round-trip
    # through the URL builder without raising.
    try:
        URL.build(scheme="ws", host=trimmed, port=80, path="/")
    except ValueError as exc:
        msg = f"manual host: 'hostname' is not a valid host: {exc}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc
    return trimmed


def _to_view(settings: RemoteBuildSettings) -> RemoteBuildSettingsView:
    """
    Project a :class:`RemoteBuildSettings` to its wire :class:`RemoteBuildSettingsView`.

    Drops the raw ``static_x25519_pub`` bytes from each peer row
    so the pubkey only stays server-side. Every controller method
    that returns settings to a client routes through here.
    """
    return RemoteBuildSettingsView(
        enabled=settings.enabled,
        manual_hosts=list(settings.manual_hosts),
        peers=[_peer_summary(p) for p in settings.peers],
    )


def _peer_summary(peer: StoredPeer) -> PeerSummary:
    """Project a :class:`StoredPeer` to wire :class:`PeerSummary`.

    Drops the raw ``static_x25519_pub`` bytes; ``pin_sha256`` is
    the wire-friendly form UIs render for OOB-verification, and
    the pubkey is only needed server-side to look up the peer
    against an incoming Noise handshake.
    """
    return PeerSummary(
        dashboard_id=peer.dashboard_id,
        pin_sha256=peer.pin_sha256,
        label=peer.label,
        paired_at=peer.paired_at,
        status=peer.status,
    )


def _find_peer_by_dashboard_id(
    settings: RemoteBuildSettings, dashboard_id: str
) -> StoredPeer | None:
    """Return the first :class:`StoredPeer` with this ``dashboard_id``, or ``None``.

    Single-pass linear scan; ``dashboard_id`` is the table's
    de-facto primary key (the receiver-UI WS commands key on it,
    the peer-link dispatcher keys on it, the offloader's polling
    loop keys on it) so a name-keyed index would just duplicate
    state. The peer table is small (one row per paired offloader),
    so the scan cost is fine and the convention "shape stays
    list-of-dataclasses on disk" outweighs the O(N) -> O(1) win
    for any production-realistic peer count.
    """
    return next((peer for peer in settings.peers if peer.dashboard_id == dashboard_id), None)


def _validate_dashboard_id(raw: object) -> str:
    """
    Validate a user-supplied ``dashboard_id`` argument.

    Same alphabet and length cap the peer-link Noise dispatcher
    enforces on the msg3-supplied ``dashboard_id`` (see
    :func:`controllers.remote_build_peer_link._dispatch_intent`);
    the regex + max-length live in :mod:`helpers.dashboard_identity`
    so the WS-command path here and the Noise-frame path can't
    drift apart.

    Rejects non-string / empty / oversized / non-base64url input
    with ``INVALID_ARGS`` rather than silently looking up nothing
    (which would yield a misleading ``NOT_FOUND``).
    """
    if not isinstance(raw, str):
        msg = "dashboard_id must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    cleaned = raw.strip()
    if (
        not cleaned
        or len(cleaned) > DASHBOARD_ID_MAX_CHARS
        or not DASHBOARD_ID_PATTERN.fullmatch(cleaned)
    ):
        msg = f"dashboard_id must be 1-{DASHBOARD_ID_MAX_CHARS} base64url chars"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return cleaned


def _validate_port(raw: object) -> int:
    """
    Validate a user-entered port number.

    ``bool`` is rejected even though ``isinstance(True, int)`` is
    true; accepting ``True`` for a port number is a footgun
    (silently coerces to 1, which IANA reserves for tcpmux).
    Range is the IANA-registered ephemeral plus
    well-known: 1-65535.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        msg = "manual host: 'port' must be an integer"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if not 1 <= raw <= 65535:
        msg = "manual host: 'port' must be between 1 and 65535"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return raw


class RemoteBuildController:
    """
    Discover peer dashboards and own the receiver-side settings.

    Constructed once in :meth:`DeviceBuilder.start`. The browser
    lifetime is tied to :meth:`start` / :meth:`stop`; the controller's
    own start happens after :class:`DevicesController.start` so the
    shared zeroconf instance is already up.
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._browser: AsyncServiceBrowser | None = None
        self._peers: dict[str, RemoteBuildPeer] = {}
        # Strong refs for fire-and-forget resolve tasks so the
        # garbage collector can't reap them mid-await.
        self._tasks: set[asyncio.Task[None]] = set()
        # The mDNS service-instance name our own ``DashboardAdvertiser``
        # publishes; captured at start so we can filter our own
        # broadcast out of the discovered list. ``None`` when the
        # advertiser was skipped (HA addon mode, zeroconf failed),
        # in which case there's nothing to filter.
        self._own_instance_name: str | None = None
        # Set while a ``rotate_identity`` call is in flight.
        # Concurrent rotations would each tear down + rebuild the
        # listener; their teardowns can interleave to leave the
        # dashboard with no listener at all, and back-to-back
        # rotations are almost always a buggy / accidental
        # double-click rather than intentional. The second caller
        # gets ``ALREADY_EXISTS`` rather than queuing — a queued
        # second rotation would silently double the
        # peer-re-pair disruption. Single-threaded asyncio
        # guarantees the check + set in :meth:`rotate_identity`
        # is atomic without an explicit lock.
        self._rotation_in_flight = False
        # Pairing window state (issue #106 design choice (c)).
        # The window narrows acceptance of ``intent="pair_request"``
        # Noise frames so an idle receiver doesn't accumulate inbox
        # noise from arbitrary LAN scanners. Already-approved peers
        # are NOT gated by the window; they connect anytime via
        # ``intent="peer_link"``.
        #
        # Refcounted by client so two browser tabs / two users with
        # the Pairing requests screen open both keep the window open
        # together. Each ``set_pairing_window(open=true)`` call adds
        # the calling WS client to the map (or refreshes its
        # last-extend timestamp); ``open=false`` removes it. The
        # window is open iff the map has any client whose last-extend
        # timestamp is within ``_PAIRING_WINDOW_DURATION_SECONDS``.
        # Crashed / disconnected clients (no graceful ``open=false``)
        # age out via the same timeout, so a one-tab close in a
        # multi-tab session doesn't immediately close the window for
        # the other tab, and a crashed tab doesn't keep the window
        # open forever. State lives in-memory only and resets on
        # dashboard restart (which is fine; admins re-open the
        # screen and the window opens fresh).
        self._pairing_window_clients: dict[Hashable, float] = {}
        # TimerHandle scheduled for the latest-extend deadline. Cancelled
        # and rescheduled on every set_pairing_window call so it always
        # tracks the "next time we need to auto-close". When the handle
        # fires, every client has aged out (any later extend would have
        # cancelled it), so the callback just clears the dict and fires
        # the close event. ``None`` when the window is closed.
        self._pairing_window_handle: asyncio.TimerHandle | None = None

    async def start(self) -> None:
        """
        Wire the browser onto the shared zeroconf and capture self-name.

        No-op when zeroconf failed to start; peer discovery is a
        nice-to-have, not load-bearing, and the controller stays in
        a "no peers, never will be" state until the next dashboard
        restart. Same fail-soft contract as
        :class:`DashboardAdvertiser`.
        """
        if self._db.devices is None:
            _LOGGER.debug("RemoteBuildController.start called before devices controller")
            return
        zeroconf = self._db.devices.zeroconf
        if zeroconf is None:
            _LOGGER.debug("zeroconf unavailable; remote-build discovery disabled")
            return
        # Capture own service-instance name so our own advertise
        # doesn't show up in ``list_hosts``. Reads through the
        # public ``service_instance_name`` accessor on
        # ``DashboardAdvertiser`` rather than reaching into
        # ``_info``; keeps this controller decoupled from the
        # advertiser's private layout.
        advertiser = self._db._dashboard_advertiser
        if advertiser is not None:
            self._own_instance_name = advertiser.service_instance_name
        # Wrap browser construction so a zeroconf-side failure (e.g.
        # the underlying socket got torn down between
        # ``DeviceStateMonitor.start`` and now, or the cache is in an
        # unexpected state) doesn't abort dashboard startup. Peer
        # discovery is fail-soft; same contract as the advertise.
        try:
            self._browser = AsyncServiceBrowser(
                zeroconf.zeroconf,
                [SERVICE_TYPE],
                handlers=[self._on_service_state_change],
            )
        except Exception:
            _LOGGER.exception("Could not start remote-build browser; peer discovery disabled")
            self._browser = None

    async def stop(self) -> None:
        """Cancel the browser and drain in-flight resolve tasks."""
        if self._browser is not None:
            try:
                await self._browser.async_cancel()
            except Exception:
                _LOGGER.debug("remote-build browser cancel failed", exc_info=True)
            self._browser = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        if self._pairing_window_handle is not None:
            self._pairing_window_handle.cancel()
            self._pairing_window_handle = None
        self._pairing_window_clients.clear()
        self._peers.clear()

    # ------------------------------------------------------------------
    # mDNS plumbing
    # ------------------------------------------------------------------

    def _on_service_state_change(
        self,
        zeroconf: Any,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """
        Browser callback; resolve the service info and update the peer map.

        Filters our own service-instance name so the advertise we
        publish doesn't show up in ``list_hosts``. ``Removed`` events
        delete the peer immediately; ``Added`` / ``Updated`` resolve
        either from the zeroconf cache (sync) or via a fire-and-forget
        task (async).
        """
        if name == self._own_instance_name:
            return
        if state_change == ServiceStateChange.Removed:
            self._peers.pop(name, None)
            return
        info = AsyncServiceInfo(service_type, name)
        if info.load_from_cache(zeroconf):
            self._peers[name] = _peer_from_service_info(name, info)
            return
        task = asyncio.create_task(self._resolve_and_apply(zeroconf, info, name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve_and_apply(self, zeroconf: Any, info: AsyncServiceInfo, name: str) -> None:
        """Async resolve path for cache misses."""
        try:
            resolved = await info.async_request(zeroconf, timeout=_RESOLVE_TIMEOUT_MS)
        except Exception:
            _LOGGER.debug("Resolve failed for %s", name, exc_info=True)
            return
        if not resolved:
            return
        self._peers[name] = _peer_from_service_info(name, info)

    # ------------------------------------------------------------------
    # API surface
    # ------------------------------------------------------------------

    @api_command("remote_build/list_hosts")
    async def list_hosts(self, **kwargs: Any) -> list[RemoteBuildPeer]:
        """
        Return every peer dashboard known to this receiver.

        Merges two sources into a single snapshot:

        * mDNS-discovered peers from the browser (``source=MDNS``,
          full version + address info).
        * Manually-added peers from
          ``_remote_build.manual_hosts`` (``source=MANUAL``, blank
          version fields until phase 4 fills them in).

        Manual hosts are placed AFTER mDNS hits so the UI's
        primary content is the auto-discovered list. A
        manually-added entry that's also reachable via mDNS shows
        up twice for now (once per source); phase 4's pairing
        flow will introduce the deduplication logic alongside the
        actual connection attempt.
        """
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        return [
            *self._peers.values(),
            *(_peer_from_manual_host(entry) for entry in settings.manual_hosts),
        ]

    @api_command("remote_build/get_settings")
    async def get_settings(self, **kwargs: Any) -> RemoteBuildSettingsView:
        """Return the receiver-side remote-build settings (wire view)."""
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        return _to_view(settings)

    async def _modify_settings(
        self, mutator: Callable[[RemoteBuildSettings], None]
    ) -> RemoteBuildSettingsView:
        """
        Run ``mutator`` against the current settings and persist the result.

        Wraps :func:`remote_build_settings_transaction` so the
        whole read-modify-write happens under the metadata lock,
        so two concurrent callers can't both read the same starting
        value and have the second save wipe the first's change.
        Runs in the default executor since the transaction does
        blocking JSON I/O. Returns the wire view so the response
        leaving this method can never carry ``secret_sha256``.

        ``mutator`` is invoked with the freshly-loaded settings
        and is expected to mutate it in place. A
        :class:`CommandError` raised inside the mutator (e.g.
        duplicate-detection on add) propagates out and discards
        the pending write; same exception-on-discard contract as
        :func:`metadata_transaction`.
        """

        def _txn() -> RemoteBuildSettings:
            with remote_build_settings_transaction(self._db.settings.config_dir) as settings:
                mutator(settings)
                return settings

        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(None, _txn)
        return _to_view(settings)

    @api_command("remote_build/set_settings")
    async def set_settings(self, *, enabled: bool, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Persist the receiver-side ``enabled`` master switch.

        Read-modify-write so manual hosts, peers, and any future
        phase-4+ fields stay intact; a client toggling just
        ``enabled`` doesn't reset every other field to its default.

        Validates ``enabled`` is strictly a ``bool`` rather than
        coercing truthiness; a client sending the string ``"false"``
        for example would otherwise persist as ``True``, which is
        the opposite of what the user intended on a security-
        sensitive toggle.

        **Listener bind requires restart.** The peer-link Noise WS
        listener is bound once in :meth:`DeviceBuilder.start` based
        on the value at startup; flipping ``enabled`` here persists
        the new value but does NOT live-rebind. The frontend should
        surface a "restart required" hint to the operator. A future
        PR can wire ``set_settings`` into the lifecycle hooks if
        interactive toggling becomes a real UX concern.
        """
        if not isinstance(enabled, bool):
            msg = "remote_build/set_settings: 'enabled' must be a boolean"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

        def _set(settings: RemoteBuildSettings) -> None:
            settings.enabled = enabled

        return await self._modify_settings(_set)

    # ------------------------------------------------------------------
    # Manual hosts (phase 2b)
    # ------------------------------------------------------------------

    @api_command("remote_build/add_manual_host")
    async def add_manual_host(
        self, *, hostname: str, port: int, **kwargs: Any
    ) -> RemoteBuildSettingsView:
        """
        Add a manually-entered peer for cross-subnet / non-mDNS LANs.

        Validates ``hostname`` (non-empty string, normalised to
        lowercase per RFC 1035 §2.3.3) and ``port`` (integer,
        1-65535). Rejects duplicates by ``(hostname, port)``:
        adding the same pair twice raises ``ALREADY_EXISTS`` so
        the frontend can render a "this dashboard is already in
        your list" message without string-matching the details
        field.

        Returns the post-write settings so the caller can re-render
        the manual-hosts list without a separate ``get_settings``
        round-trip.
        """
        host = _validate_hostname(hostname)
        port_num = _validate_port(port)

        def _add(settings: RemoteBuildSettings) -> None:
            for entry in settings.manual_hosts:
                if entry.hostname == host and entry.port == port_num:
                    msg = f"manual host {host}:{port_num} is already registered"
                    raise CommandError(ErrorCode.ALREADY_EXISTS, msg)
            settings.manual_hosts.append(ManualHost(hostname=host, port=port_num))

        return await self._modify_settings(_add)

    @api_command("remote_build/remove_manual_host")
    async def remove_manual_host(
        self, *, hostname: str, port: int, **kwargs: Any
    ) -> RemoteBuildSettingsView:
        """
        Remove a previously-added manual peer.

        Hostname normalisation matches :meth:`add_manual_host` so a
        case-different removal request finds the entry. A
        non-existent ``(hostname, port)`` pair raises
        ``NOT_FOUND`` so the caller knows the operation was a no-op
        rather than silently succeeding (matters for the
        Settings UI: "Removed Foo" toast vs no feedback).
        """
        host = _validate_hostname(hostname)
        port_num = _validate_port(port)

        def _remove(settings: RemoteBuildSettings) -> None:
            kept = [
                entry
                for entry in settings.manual_hosts
                if not (entry.hostname == host and entry.port == port_num)
            ]
            if len(kept) == len(settings.manual_hosts):
                msg = f"manual host {host}:{port_num} is not registered"
                raise CommandError(ErrorCode.NOT_FOUND, msg)
            settings.manual_hosts = kept

        return await self._modify_settings(_remove)

    # ------------------------------------------------------------------
    # Offloader-side pair flow (phase 4a-o) — initiator commands that
    # open Noise XX WebSockets to a receiver's peer-link endpoint. The
    # wire-shape driver lives in
    # :mod:`controllers.remote_build_peer_link_client`; the WS command
    # here owns input validation, identity loading, and error mapping.
    # ------------------------------------------------------------------

    @api_command("remote_build/preview_pair")
    async def preview_pair(self, *, hostname: str, port: int, **kwargs: Any) -> dict[str, str]:
        """Open a brief Noise XX WS to *hostname*:*port* and return the receiver's pin.

        The offloader runs ``intent="preview"`` to capture the
        receiver's static X25519 pubkey from the Noise handshake
        transcript before committing to pair. The frontend
        renders the returned ``pin_sha256`` for the user to
        OOB-verify against the receiver's "Build server"
        Settings card; only after that confirmation does the
        offloader call ``request_pair`` (phase 4a-o part 3).

        Args:
            hostname: Receiver's hostname / IP (validated as for
                manual hosts: non-empty, ≤255 chars,
                lowercase-normalised).
            port: Receiver's peer-link port (1-65535, non-bool).

        Returns:
            ``{"pin_sha256": "<lowercase-hex-64>"}`` on a
            successful preview round-trip.

        Raises:
            :class:`CommandError(INVALID_ARGS)` for bad inputs.
            :class:`CommandError(UNAVAILABLE)` for any transport
            / handshake / decode failure (connection refused,
            timeout, malformed Noise frame, mismatched
            ``intent_response``).
        """
        clean_host = _validate_hostname(hostname)
        clean_port = _validate_port(port)
        loop = asyncio.get_running_loop()
        identity = await loop.run_in_executor(
            None,
            get_or_create_peer_link_identity,
            self._db.settings.config_dir,
        )
        try:
            pin = await peer_link_preview_pair(
                hostname=clean_host,
                port=clean_port,
                identity_priv=identity.private_bytes,
            )
        except PeerLinkClientError as exc:
            raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc
        return {"pin_sha256": pin}

    # ------------------------------------------------------------------
    # Identity (phase 3c1) — surface the receiver's own dashboard_id +
    # cert pin to the Settings UI without making it reach into the
    # cert PEM directly. Rotation lives next door so the "rotate"
    # button can land in the same controller.
    # ------------------------------------------------------------------

    @api_command("remote_build/get_identity")
    async def get_identity(self, **kwargs: Any) -> IdentityView:
        """
        Return this dashboard's stable identity (id + cert pin + versions).

        Reads the persistent identity via
        :func:`helpers.dashboard_identity.get_or_create_identity`
        — idempotent, and lazy-creates the cert + key pair if
        missing. ``listener_bound`` reports whether the
        peer-link Noise WS listener is currently serving
        traffic. The cert + key PEMs themselves are intentionally
        NOT returned; only the SPKI fingerprint (``pin_sha256``)
        is safe to ship to a frontend, and the fingerprint is
        what an offloader pins against anyway.

        ``server_version`` and ``esphome_version`` ride on the
        same response so the Settings UI can render the "Build
        host" card from a single WS call instead of hopping
        through the existing ``firmware/get_versions``-style
        commands.
        """
        loop = asyncio.get_running_loop()
        identity = await loop.run_in_executor(
            None, get_or_create_identity, self._db.settings.config_dir
        )
        return _identity_view(identity, listener_bound=self._db.is_remote_build_listener_bound)

    @api_command("remote_build/rotate_identity")
    async def rotate_identity(self, **kwargs: Any) -> IdentityView:
        """
        Mint a fresh cert + key pair, replacing whatever's on disk.

        Forces every paired offloader to re-pair: the new SPKI
        produces a new ``pin_sha256``, and any peer that pinned
        the old one will see a fingerprint mismatch on the next
        TLS handshake (peer-link work in phase 5+ surfaces this
        through a re-verify wizard). The ``dashboard_id`` is
        preserved so the receiver-side audit trail stays
        readable across rotations.

        Side effects: (1) the bound TCP site is torn down and
        rebuilt with a fresh SSL context if remote-build is
        currently enabled and bound; the rebuild fail-softs
        (``listener_bound=False`` in the response) so the
        Settings UI can show "rotation succeeded but the
        listener didn't come back up — check logs". (2) The
        mDNS advertise picks up the new ``pin_sha256`` either
        way so peers re-browsing see the rotation even when the
        listener wasn't bound. (3) An
        :attr:`EventType.REMOTE_BUILD_IDENTITY_ROTATED` event
        fires on the bus carrying ``{dashboard_id, pin_sha256}``
        so subscribers (the offloader-side peer-link in 4+, the
        receiver Settings UI in 3c2) can refresh without
        polling ``get_identity``.

        **Concurrent calls fail with ``ALREADY_EXISTS``.** Two
        rotations racing would each tear down + rebuild the
        listener, and back-to-back rotation is almost always an
        accidental double-click rather than two intentional
        events; the frontend is expected to confirm before each
        call. Rotation is otherwise intentionally cheap to
        invoke (Ed25519 keygen + a couple of disk writes),
        bounded only by the WS auth gate on this command's
        channel.
        """
        # Single-threaded asyncio guarantees the check + set is
        # atomic — no other coroutine runs between these two
        # statements without an ``await``.
        if self._rotation_in_flight:
            msg = "remote_build: an identity rotation is already in progress"
            raise CommandError(ErrorCode.ALREADY_EXISTS, msg)
        self._rotation_in_flight = True
        try:
            loop = asyncio.get_running_loop()
            identity = await loop.run_in_executor(
                None, rotate_certificate, self._db.settings.config_dir
            )
            listener_bound = await self._db.reload_remote_build_identity(
                pin_sha256=identity.pin_sha256,
            )
            self._db.bus.fire(
                EventType.REMOTE_BUILD_IDENTITY_ROTATED,
                {
                    "dashboard_id": identity.dashboard_id,
                    "pin_sha256": identity.pin_sha256,
                },
            )
            return _identity_view(identity, listener_bound=listener_bound)
        finally:
            self._rotation_in_flight = False

    # ------------------------------------------------------------------
    # Peer CRUD (phase 4a-r1 part 3) — receiver-UI surface for the
    # Pairing requests inbox and the approved-peers list. The peer-link
    # listener (phase 4a-r1 part 4) is the actual creator of PENDING
    # rows; these commands are the receiver-side admin's UI surface for
    # acting on them.
    # ------------------------------------------------------------------

    @api_command("remote_build/list_peers")
    async def list_peers(self, **kwargs: Any) -> list[PeerSummary]:
        """
        Return every ``StoredPeer`` row, projected to wire shape.

        Includes both PENDING (waiting for admin Accept) and APPROVED
        (paired) rows; the wire view drops the raw ``static_x25519_pub``
        bytes and exposes only ``pin_sha256``. The frontend filters by
        ``status`` to render the inbox vs the paired list.
        """
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        return [_peer_summary(peer) for peer in settings.peers]

    @api_command("remote_build/approve_peer")
    async def approve_peer(self, *, dashboard_id: str, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Promote a PENDING peer to APPROVED.

        Fires :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED` with
        ``{dashboard_id, status: "approved"}``. The offloader observes
        the flip via its own polling loop (phase 4a-o ``list_pool``).
        ``NOT_FOUND`` if no peer with this ``dashboard_id`` exists;
        ``INVALID_ARGS`` if the peer is already APPROVED (a duplicate
        Accept click is almost always a UI race and the receiver
        should not silently re-fire the event).
        """
        clean_id = _validate_dashboard_id(dashboard_id)

        def _approve(settings: RemoteBuildSettings) -> None:
            peer = _find_peer_by_dashboard_id(settings, clean_id)
            if peer is None:
                msg = f"no peer with dashboard_id: {clean_id}"
                raise CommandError(ErrorCode.NOT_FOUND, msg)
            if peer.status == PeerStatus.APPROVED:
                msg = f"peer is already approved: {clean_id}"
                raise CommandError(ErrorCode.INVALID_ARGS, msg)
            peer.status = PeerStatus.APPROVED

        view = await self._modify_settings(_approve)
        self._fire_pair_status_changed(clean_id, "approved")
        return view

    @api_command("remote_build/remove_peer")
    async def remove_peer(self, *, dashboard_id: str, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Delete a peer row (works on both PENDING and APPROVED).

        Two semantically distinct outcomes share the same WS command:

        * Removing a PENDING row is *rejection* — the row never
          represented an established trust relationship, so this is
          inbox cleanup. No event fires.
        * Removing an APPROVED row is *revocation* — fires
          :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED` with
          ``{dashboard_id, status: "removed"}`` so the offloader's
          polling loop sees the revocation and can surface a
          ``peer_revoked`` UI alert (phase 4b-3).

        ``NOT_FOUND`` if no peer with this ``dashboard_id`` exists.
        """
        clean_id = _validate_dashboard_id(dashboard_id)
        # ``_modify_settings`` returns the view but doesn't tell us
        # what the peer's status was *before* the remove; capture
        # that in the mutator so the event-fire decision below has
        # the right answer.
        previous_status: PeerStatus | None = None

        def _remove(settings: RemoteBuildSettings) -> None:
            nonlocal previous_status
            peer = _find_peer_by_dashboard_id(settings, clean_id)
            if peer is None:
                msg = f"no peer with dashboard_id: {clean_id}"
                raise CommandError(ErrorCode.NOT_FOUND, msg)
            previous_status = peer.status
            settings.peers = [p for p in settings.peers if p.dashboard_id != clean_id]

        view = await self._modify_settings(_remove)
        if previous_status == PeerStatus.APPROVED:
            self._fire_pair_status_changed(clean_id, "removed")
        return view

    # ------------------------------------------------------------------
    # Peer-link Noise WS dispatch helpers (phase 4a-r1 part 4) — called
    # by the post-handshake intent dispatcher in
    # :mod:`controllers.remote_build_peer_link`. These methods own the
    # storage / event-firing side; the dispatcher owns the wire side.
    # ------------------------------------------------------------------

    async def record_pair_request(
        self,
        *,
        dashboard_id: str,
        pin_sha256: str,
        static_x25519_pub: bytes,
        label: str,
        peer_ip: str,
    ) -> IntentResponse:
        """
        Process an ``intent="pair_request"`` Noise session.

        Caller is expected to have already gated on
        :meth:`is_pairing_window_open` — this method does NOT
        re-check, so a window-closed dispatch should never reach
        here.

        Returns:
        * :attr:`IntentResponse.PENDING` — created a new
          ``StoredPeer`` (or refreshed an existing PENDING row's
          pin / label / paired_at). Fires
          :attr:`EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED` so
          the receiver UI surfaces the request in the inbox.
        * :attr:`IntentResponse.APPROVED` — a row already exists
          for this ``dashboard_id`` with status APPROVED **and**
          its stored pin matches the handshake's. Returns
          ``APPROVED`` without changing the row or firing the
          event; demoting an already-trusted peer back to PENDING
          on every stray pair_request would force the receiving
          dashboard's user to re-approve on every offloader
          hiccup, which is hostile UX.
        * :attr:`IntentResponse.REJECTED` — a row exists for this
          ``dashboard_id`` with status APPROVED but the
          handshake's pin doesn't match the stored pin. Either
          the offloader rotated their identity under us, or
          someone is presenting a fresh keypair and claiming
          Alice's ``dashboard_id``. Refuse the operation; the
          receiver-side user has to remove the peer and re-pair
          if the rotation is legitimate.
        """
        outcome = _PairRequestOutcome()

        def _record(settings: RemoteBuildSettings) -> None:
            now = time.time()
            peer = _find_peer_by_dashboard_id(settings, dashboard_id)
            if peer is None:
                settings.peers.append(
                    StoredPeer(
                        dashboard_id=dashboard_id,
                        pin_sha256=pin_sha256,
                        static_x25519_pub=static_x25519_pub,
                        label=label,
                        paired_at=now,
                        status=PeerStatus.PENDING,
                    )
                )
                outcome.response = IntentResponse.PENDING
                return
            if peer.status == PeerStatus.APPROVED:
                if peer.pin_sha256 != pin_sha256:
                    # Pin mismatch on an APPROVED row is a
                    # rotation-or-impersonation signal; refuse
                    # rather than silently re-approve under the
                    # new identity.
                    outcome.response = IntentResponse.REJECTED
                    return
                outcome.response = IntentResponse.APPROVED
                return
            # PENDING: refresh in place. The pin / pubkey may
            # have changed (offloader rotated), the label may
            # have changed (user renamed the dashboard before
            # they clicked Accept). Keep the row's status =
            # PENDING; the user re-Accepts when ready.
            peer.refresh_from_pair_request(
                pin_sha256=pin_sha256,
                static_x25519_pub=static_x25519_pub,
                label=label,
                paired_at=now,
            )
            outcome.response = IntentResponse.PENDING

        await self._modify_settings(_record)
        # Every branch of ``_record`` sets ``outcome.response``;
        # the type narrowing is for mypy / pyright only, gated
        # behind ``TYPE_CHECKING`` so it costs nothing at runtime.
        if TYPE_CHECKING:
            assert outcome.response is not None
        if outcome.response is not IntentResponse.PENDING:
            return outcome.response

        payload: RemoteBuildPairRequestReceivedData = {
            "dashboard_id": dashboard_id,
            "pin_sha256": pin_sha256,
            "label": label,
            "peer_ip": peer_ip,
        }
        self._db.bus.fire(EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED, payload)
        return outcome.response

    async def lookup_peer_for_session(
        self,
        *,
        dashboard_id: str,
        pin_sha256: str,
    ) -> IntentResponse:
        """
        Resolve an ``intent="peer_link"`` request.

        Returns:
        * :attr:`IntentResponse.OK` — peer is APPROVED and the
          handshake's pubkey hash matches the stored
          ``pin_sha256``. Caller can keep the WS open for
          application messages (phase 5+).
        * :attr:`IntentResponse.PENDING` — peer's row exists but
          status is PENDING (the receiver-side user hasn't
          clicked Accept yet). Offloader's UI keeps polling.
        * :attr:`IntentResponse.REJECTED` — no row matches OR the
          row's stored ``pin_sha256`` doesn't match the
          handshake's. Either the offloader has never paired
          (unknown), or the offloader's peer-link identity
          rotated under us, or someone is claiming Alice's
          ``dashboard_id`` with their own keys. The offloader
          treats this as "send a fresh pair_request".
        """
        return await self._lookup_peer_response(
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            approved_response=IntentResponse.OK,
        )

    async def lookup_peer_for_status(
        self,
        *,
        dashboard_id: str,
        pin_sha256: str,
    ) -> IntentResponse:
        """
        Resolve an ``intent="pair_status"`` poll query.

        Returns:
        * :attr:`IntentResponse.APPROVED` — peer is APPROVED.
        * :attr:`IntentResponse.PENDING` — peer's row exists but
          status is PENDING.
        * :attr:`IntentResponse.REJECTED` — no row matches OR pin
          mismatch. Caller's frontend interprets this as "the row
          was rejected / revoked / never existed; surface
          ``peer_revoked`` UI".

        Differs from :meth:`lookup_peer_for_session` only in the
        APPROVED-state wire member (``APPROVED`` vs ``OK``) because
        pair_status is informational while peer_link is
        connection-establishing.
        """
        return await self._lookup_peer_response(
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            approved_response=IntentResponse.APPROVED,
        )

    async def _lookup_peer_response(
        self,
        *,
        dashboard_id: str,
        pin_sha256: str,
        approved_response: IntentResponse,
    ) -> IntentResponse:
        """
        Shared lookup core for the peer_link / pair_status WS dispatch paths.

        Both wire intents do the same lookup ("find this
        ``dashboard_id`` and verify its stored pin matches the
        handshake's") and differ only in what they return for an
        APPROVED match. Pulling that one variation out as
        ``approved_response`` keeps the logic in one place; a
        future change (e.g. constant-time pin compare, log
        mismatches, fire a pin-mismatch event) lands on one
        method, not two.
        """
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        peer = _find_peer_by_dashboard_id(settings, dashboard_id)
        if peer is None or peer.pin_sha256 != pin_sha256:
            return IntentResponse.REJECTED
        if peer.status == PeerStatus.APPROVED:
            return approved_response
        return IntentResponse.PENDING

    # ------------------------------------------------------------------
    # Pairing window (phase 4a-r1 part 3) — in-process deadline that
    # gates ``intent="pair_request"`` Noise frames at the listener
    # (phase 4a-r1 part 4 consumes :meth:`is_pairing_window_open`).
    # See issue #106 design choice (c).
    # ------------------------------------------------------------------

    @api_command("remote_build/set_pairing_window")
    async def set_pairing_window(
        self,
        *,
        open: bool,  # noqa: A002 — wire format names this field "open"
        client: Hashable,
        **kwargs: Any,
    ) -> PairingWindowState:
        """
        Open, extend, or close the pairing window for the calling client.

        Wire shape: ``{open: bool}``. Refcounted by WS client: each
        ``open=true`` adds (or refreshes) the caller's entry in the
        active-clients map; ``open=false`` removes it. The window is
        open iff *any* client has a non-stale entry. The
        receiver-side frontend calls this on screen-mount and on
        each activity-driven extend tick (debounced to once per 30s
        on the wire), and ``open=false`` on screen-unmount /
        ``beforeunload``. An explicit "extend" / "still pairing?"
        button in the UI is just another caller of ``open=true``;
        no separate wire command is needed for it.

        ``client`` is the WS connection object that the dispatcher
        injects on every command call (see ``api/ws.py``); we use
        the connection itself as the refcount dict key, so two
        browser tabs / two users get distinct entries. Required
        kwarg with no default: a missing ``client`` would silently
        bucket every caller under the same key and break the
        refcount, so we want the loud ``TypeError`` from a missing
        kwarg instead. Tests pass a stand-in hashable (``"tab-1"``,
        etc.) to simulate distinct clients.

        Fires :attr:`EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED` on
        every state transition. Idempotent calls that don't change
        state (close-while-already-closed; or close from a client
        that wasn't extending while another client still is) do NOT
        fire; the frontend renders countdown ticks client-side and
        doesn't need a per-second fire.

        Two-tab / two-user behaviour: window stays open as long as
        at least one client is extending. A crashed tab ages out
        naturally via the 5min idle timeout (no per-client
        disconnect hook is needed); a graceful close from one tab
        leaves the window open for the other tab. See issue #106
        design choice (c).
        """
        if not isinstance(open, bool):
            msg = "remote_build/set_pairing_window: 'open' must be a bool"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

        was_open = self.is_pairing_window_open()
        if open:
            self._pairing_window_clients[client] = time.monotonic()
        else:
            self._pairing_window_clients.pop(client, None)
        # Cancel the existing handle and schedule a new one against
        # the current latest-extend deadline. When the dict is empty
        # (last client closed), no new handle is scheduled; this is
        # what prevents a duplicate close event from a stale handle
        # on the explicit-close path.
        self._reschedule_pairing_window_close()
        is_open = bool(self._pairing_window_clients)

        # Fire on state transitions, AND on every successful extend
        # (open=True with the window already open) so the frontend's
        # live countdown re-syncs against the bumped deadline. A
        # spurious open=False from a non-extending client (no state
        # change) doesn't fire.
        if was_open != is_open or (open and is_open):
            self._fire_pairing_window_changed()
        return self._pairing_window_state()

    def is_pairing_window_open(self) -> bool:
        """
        Return whether the pairing window is currently open.

        Consumed by the peer-link listener (phase 4a-r1 part 4) to
        gate ``intent="pair_request"`` Noise frames. A closed window
        rejects the frame with ``intent_response=no_pairing_window``
        and closes the WS without creating a row.
        """
        self._prune_stale_pairing_window_clients()
        return bool(self._pairing_window_clients)

    def _pairing_window_remaining(self) -> float | None:
        """
        Seconds until the latest-extend deadline, or ``None`` if closed.

        Single source of truth for the deadline math: prunes stale
        clients first, then derives the remaining lifetime from the
        most recent extend across all live clients. Consumed by both
        the wire-projection (:meth:`_pairing_window_state`) and the
        TimerHandle scheduler (:meth:`_reschedule_pairing_window_close`)
        so they can't drift out of sync on the cutoff calculation.
        """
        self._prune_stale_pairing_window_clients()
        if not self._pairing_window_clients:
            return None
        latest_extend = max(self._pairing_window_clients.values())
        return max(0.0, latest_extend + _PAIRING_WINDOW_DURATION_SECONDS - time.monotonic())

    def _pairing_window_state(self) -> PairingWindowState:
        """Project the in-memory client map into a wire-shape response."""
        remaining = self._pairing_window_remaining()
        if remaining is None:
            return PairingWindowState(open=False, expires_in_seconds=None)
        return PairingWindowState(open=True, expires_in_seconds=remaining)

    def _fire_pair_status_changed(
        self, dashboard_id: str, status: Literal["approved", "removed"]
    ) -> None:
        """
        Fire ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` for a peer transition.

        ``status`` is ``"approved"`` (from :meth:`approve_peer`) or
        ``"removed"`` (from :meth:`remove_peer` of a previously-
        APPROVED row). Mirrors :meth:`_fire_pairing_window_changed`
        for shape; both methods are the named-intent boundary
        between controller logic and the bus payload format.
        """
        payload: RemoteBuildPairStatusChangedData = {
            "dashboard_id": dashboard_id,
            "status": status,
        }
        self._db.bus.fire(EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED, payload)

    def _fire_pairing_window_changed(self) -> None:
        """Fire ``REMOTE_BUILD_PAIRING_WINDOW_CHANGED`` with the current state."""
        state = self._pairing_window_state()
        payload: RemoteBuildPairingWindowChangedData = {
            "open": state.open,
            "expires_in_seconds": state.expires_in_seconds,
        }
        self._db.bus.fire(EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED, payload)

    def _prune_stale_pairing_window_clients(self) -> None:
        """Drop client entries whose last-extend timestamp aged out."""
        if not self._pairing_window_clients:
            return
        cutoff = time.monotonic() - _PAIRING_WINDOW_DURATION_SECONDS
        self._pairing_window_clients = {
            client: extended_at
            for client, extended_at in self._pairing_window_clients.items()
            if extended_at >= cutoff
        }

    def _reschedule_pairing_window_close(self) -> None:
        """
        Cancel any pending close handle and schedule a fresh one.

        Called after every :meth:`set_pairing_window` mutation. The
        handle always reflects the current latest-extend deadline,
        so on every extend we cancel and reschedule rather than
        letting an old handle wake up and re-check; this avoids the
        duplicate-close-event class of bug where an old handle
        would fire after an explicit close.

        When the client map is empty (the explicit-close case where
        the last client just dropped out), no new handle is
        scheduled and ``_pairing_window_handle`` stays ``None``.
        """
        if self._pairing_window_handle is not None:
            self._pairing_window_handle.cancel()
            self._pairing_window_handle = None
        remaining = self._pairing_window_remaining()
        if remaining is None:
            return
        loop = asyncio.get_running_loop()
        self._pairing_window_handle = loop.call_later(remaining, self._on_pairing_window_deadline)

    def _on_pairing_window_deadline(self) -> None:
        """
        Sync callback fired by the TimerHandle when the deadline lapses.

        The handle was scheduled to the latest-extend deadline; if
        any later extend had bumped the deadline, the handle would
        have been cancelled and rescheduled, so by the time we run
        every client has aged out. Clear the dict, fire the close
        event, done. No re-check loop needed (which is the whole
        point of TimerHandle vs an asyncio.sleep coroutine).
        """
        self._pairing_window_handle = None
        self._pairing_window_clients.clear()
        self._fire_pairing_window_changed()


def _identity_view(identity: DashboardIdentity, *, listener_bound: bool) -> IdentityView:
    """Project a :class:`DashboardIdentity` into the wire shape."""
    return IdentityView(
        dashboard_id=identity.dashboard_id,
        pin_sha256=identity.pin_sha256,
        server_version=server_version,
        esphome_version=esphome_version,
        listener_bound=listener_bound,
    )
