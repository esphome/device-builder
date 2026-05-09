"""
Remote-build feature; peer dashboard discovery + settings + tokens.

Browses ``_esphomebuilder._tcp.local.`` to list other dashboards
reachable on the LAN; persists the receiver-side ``enabled``
master switch, the user-supplied manual-host list for
cross-subnet / non-multicast LANs, and the receiver-issued
bearer-token list; merges discovery sources into a single
``remote_build/list_hosts`` snapshot.

The ``enabled`` flag gates the HTTPS receiver site
:class:`DeviceBuilder` binds at startup (``/remote-build/v1/*``,
default port 6055). Toggling ``enabled`` at runtime persists
the new value but does NOT live-bind / unbind the listener;
flipping it requires a dashboard restart for the listener
state to follow. The 3c Settings UI surfaces this constraint;
a future PR can wire the start / stop hooks if interactive
toggling matters.

Tokens are validated by the auth middleware on that site
against an in-memory index seeded from disk in :meth:`start`
and refreshed on every CRUD mutation. **The frontend
generates the bearer (token_id + secret) client-side** and
submits only ``SHA-256(secret)`` to the backend; the
cleartext bearer never crosses the wire from frontend to
dashboard. This closes the leak that would otherwise occur
when the dashboard is reachable on plain HTTP (standalone
``--host 0.0.0.0`` without a reverse-proxy TLS terminator).
Only the hash lands on disk; if the user loses the cleartext,
the recovery is to remove the token and register a fresh one.

Pairing flow + peer-link WS arrive in later phases. The
listener currently serves only ``/remote-build/v1/health`` as
a smoke endpoint; phase 5+ adds the real bundle / build /
firmware RPCs against the same auth surface.

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
import re
import time
from collections.abc import Callable, Hashable
from typing import TYPE_CHECKING, Any

from esphome.const import __version__ as esphome_version
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
from ..models import (
    ErrorCode,
    EventType,
    IdentityView,
    ManualHost,
    PairingWindowState,
    PeerStatus,
    PeerSummary,
    RemoteBuildPeer,
    RemoteBuildPeerSource,
    RemoteBuildSettings,
    RemoteBuildSettingsView,
    StoredPeer,
    StoredToken,
    TokenSummary,
)
from .config import load_remote_build_settings, remote_build_settings_transaction

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


def _validate_hostname(raw: object) -> str:
    """
    Normalise a user-entered hostname to its canonical lowercase form.

    Rejects non-string and empty / whitespace-only input with
    :class:`CommandError(INVALID_ARGS)`. Lowercase normalisation
    matches the duplicate-check semantics; hostnames are
    case-insensitive per RFC 1035 §2.3.3, so ``Desktop.local`` and
    ``desktop.local`` should be the same entry. The stored form
    is the trimmed, lowercased string (so two adds with different
    casing collapse to one entry rather than registering twice).
    Phase 4 attempts the actual connection (and discovers DNS /
    TLS validity); phase 2b deliberately doesn't pre-flight an
    "is this resolvable now?" check, which would fail on offline
    laptops adding a peer for later.
    """
    if not isinstance(raw, str):
        msg = "manual host: 'hostname' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip().lower()
    if not trimmed:
        msg = "manual host: 'hostname' must not be empty"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return trimmed


# Wire format for the bearer the offloader presents is
# ``{token_id}.{secret}``: a fixed 11-char base64url ``token_id``
# (the textual form of 8 random bytes — 64 bits, plenty against
# birthday collisions at the ``_MAX_TOKENS = 100`` cap) plus a
# 43-char base64url ``secret`` (the textual form of 32 random
# bytes — 256 bits, infeasible to brute force). Both halves are
# base64url so the wire form has no shell-quoting hazards.
# ``_validate_token_id`` enforces the exact 11-char length so
# the collision math stays load-bearing — without that pin, a
# client could ship arbitrary-length ids and the backend's
# bookkeeping would still work but the entropy guarantee
# wouldn't.
#
# **The cleartext bearer is never sent to the backend.** The
# frontend generates ``token_id`` + ``secret`` client-side
# (``crypto.getRandomValues`` — the only acceptable source;
# any fallback to ``Math.random`` or similar is a security
# regression because the backend has no way to verify the
# entropy of a hash, only its shape), computes
# ``SHA-256(secret)`` locally, and POSTs
# ``{label, token_id, secret_sha256}``. The backend stores only
# the hash; the cleartext bearer stays on the user's screen
# long enough to copy into the offloader, then discarded. This
# closes the leak that would otherwise occur when the dashboard
# is reachable on plain HTTP (for example a standalone
# ``--host 0.0.0.0`` deployment without a reverse-proxy TLS
# terminator).

# Cap label length to keep ``list_tokens`` payloads bounded and
# prevent a misbehaving frontend from accidentally storing a
# multi-megabyte string. Generous enough for "Green dashboard
# (kitchen)" style labels.
_TOKEN_LABEL_MAX = 128

# ``secrets.token_urlsafe`` emits the base64url alphabet only;
# any other character means the caller is sending something that
# isn't a token_id (most likely the full bearer or a typo). Pin
# the exact length to ``base64url(8 bytes) = 11`` so the
# collision math stays load-bearing — a client that shipped
# longer / shorter ids would still work for storage but the
# 64-bit entropy guarantee wouldn't.
_TOKEN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_TOKEN_ID_LEN = 11

# Soft cap on the receiver-side token list so a misbehaving
# frontend looping ``add_token`` can't grow the metadata file
# unboundedly. 100 is well above any realistic
# pairings-per-receiver count and gives the UI a clean upper
# bound to render.
_MAX_TOKENS = 100


def _validate_label(raw: object) -> str:
    """
    Normalise a user-entered token label to a stripped, length-bounded form.

    Rejects non-string, empty / whitespace-only, and too-long
    inputs with :class:`CommandError(INVALID_ARGS)`. Duplicate
    labels are NOT rejected: ``token_id`` is the unique key, and
    a user might legitimately want two tokens both labelled
    "Green" (different machines or different purposes).
    """
    if not isinstance(raw, str):
        msg = "token: 'label' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip()
    if not trimmed:
        msg = "token: 'label' must not be empty"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if len(trimmed) > _TOKEN_LABEL_MAX:
        msg = f"token: 'label' must be at most {_TOKEN_LABEL_MAX} characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return trimmed


def _validate_token_id(raw: object) -> str:
    """
    Validate a user-supplied token_id.

    Pins the exact 11-char length so the 64-bit entropy
    guarantee in the bearer-format docstring stays accurate;
    the frontend's ``crypto.getRandomValues(new Uint8Array(8))``
    + base64url encode produces 11 chars, and ``secrets.token_urlsafe(8)``
    on the test side does the same. A shorter id shrinks the
    namespace; a longer one isn't generated by any honest
    client and almost certainly indicates the caller stuffed
    extra data in.

    Also rejects values containing ``.``: the bearer wire form
    is ``{token_id}.{secret}``, so a value with a dot is most
    likely the full bearer mistakenly passed instead of the id
    half. Rejecting before the value lands in any error message
    keeps the cleartext secret out of logs / DevTools / frontend
    telemetry.

    Shape-checks only; the existence check happens in the mutator
    under the metadata lock so look-up and delete are atomic.
    """
    if not isinstance(raw, str):
        msg = "token: 'token_id' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip()
    if not trimmed:
        msg = "token: 'token_id' must not be empty"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if "." in trimmed:
        # Specifically don't echo ``trimmed`` back: it might be a
        # full bearer, in which case the secret half is in this
        # variable.
        msg = "token: 'token_id' must be the id half only, not the full bearer"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if len(trimmed) != _TOKEN_ID_LEN:
        msg = f"token: 'token_id' must be exactly {_TOKEN_ID_LEN} characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if not _TOKEN_ID_PATTERN.fullmatch(trimmed):
        msg = "token: 'token_id' must contain base64url characters only"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return trimmed


_SECRET_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_secret_sha256(raw: object) -> str:
    """
    Validate a client-supplied SHA-256 hash of the bearer secret half.

    Must be exactly 64 lowercase hex chars (the textual form of
    SHA-256). Catches frontend bugs that send the cleartext
    secret instead of the hash, send an uppercase / mixed-case
    digest, or send a different-length string.

    The frontend computes ``sha256(secret)`` client-side so the
    cleartext bearer never crosses the wire to the backend; the
    backend persists only this hash. Defending against
    malformed-input here is a sanity check, not a security
    boundary — even a valid-shape hash with no matching cleartext
    is just an unusable token row.
    """
    if not isinstance(raw, str):
        msg = "token: 'secret_sha256' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip()
    if not _SECRET_SHA256_PATTERN.fullmatch(trimmed):
        msg = "token: 'secret_sha256' must be 64 lowercase hex characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return trimmed


def _summarise_token(token: StoredToken) -> TokenSummary:
    """Project a :class:`StoredToken` to its public-facing :class:`TokenSummary`."""
    return TokenSummary(
        token_id=token.token_id,
        label=token.label,
        created_at=token.created_at,
        bound_dashboard_id=token.bound_dashboard_id,
    )


def _to_view(settings: RemoteBuildSettings) -> RemoteBuildSettingsView:
    """
    Project a :class:`RemoteBuildSettings` to its wire :class:`RemoteBuildSettingsView`.

    Drops ``secret_sha256`` from each token row and the raw
    ``static_x25519_pub`` bytes from each peer row. Every
    controller method that returns settings to a client routes
    through here so the stored secrets never leave the server,
    even when a CRUD response also touches the tokens or peers
    list.
    """
    return RemoteBuildSettingsView(
        enabled=settings.enabled,
        manual_hosts=list(settings.manual_hosts),
        tokens=[_summarise_token(t) for t in settings.tokens],
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


def _validate_dashboard_id(raw: object) -> str:
    """
    Validate a user-supplied ``dashboard_id`` argument.

    Same alphabet and length cap as the phase 3a / 3b3 ``X-Dashboard-ID``
    header contract; the regex + max-length live in
    :mod:`helpers.dashboard_identity` so the WS-command path here
    and the HTTP-header path in :mod:`helpers.remote_build_auth`
    can't drift apart.

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
        # In-memory token index keyed off ``token_id``. Built from
        # disk at start; refreshed after every CRUD mutation so
        # the auth middleware's lookup is constant-time and
        # never has to hit the filesystem on the request hot
        # path. Empty until ``start`` runs.
        self._tokens_by_id: dict[str, StoredToken] = {}
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
        # Seed the token index from disk before the zeroconf gate
        # below: the index is consumed by the HTTPS auth middleware
        # (phase 3b2), not by the zeroconf browser. Even on a
        # zeroconf-disabled deployment (HA addon, container with
        # mDNS broken) the index needs to be live so the listener
        # can validate bearers.
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        self._tokens_by_id = {t.token_id: t for t in settings.tokens}

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
        # Keep the auth middleware's lookup index in sync with the
        # post-write state. Add / remove / first-use-binding all
        # route through here, so this is the one place that needs
        # to refresh.
        self._tokens_by_id = {t.token_id: t for t in settings.tokens}
        return _to_view(settings)

    def lookup_token(self, token_id: str) -> StoredToken | None:
        """
        Return the matching :class:`StoredToken` for ``token_id``, or ``None``.

        Public accessor for the phase-3b2 auth middleware. Reads
        the in-memory index (constant-time dict hit, no I/O on
        the request hot path). The index is seeded in
        :meth:`start` and kept in sync via
        :meth:`_modify_settings`.
        """
        return self._tokens_by_id.get(token_id)

    async def bind_token_first_use(self, token_id: str, dashboard_id: str) -> StoredToken | None:
        """
        Atomically bind ``token_id`` to ``dashboard_id`` on first authenticated use.

        Returns the post-write :class:`StoredToken` (with
        ``bound_dashboard_id`` populated), or ``None`` if the
        token has been removed in the meantime.

        Idempotent: if the token is already bound, the existing
        ``bound_dashboard_id`` is preserved and the call is a
        no-op write. Two concurrent first-use requests with
        different ``dashboard_id`` values race for the slot; the
        winner's id sticks, the loser's call returns the
        winner-bound token. Callers compare the returned
        ``bound_dashboard_id`` against the value they presented
        to detect a race-loss → 403 mismatch.

        Phase-3b2's auth middleware is the only caller. The
        write hops through ``run_in_executor`` because the
        underlying ``metadata_transaction`` is sync filesystem
        I/O.
        """

        def _bind(settings: RemoteBuildSettings) -> StoredToken | None:
            for token in settings.tokens:
                if token.token_id != token_id:
                    continue
                if token.bound_dashboard_id is None:
                    token.bound_dashboard_id = dashboard_id
                return token
            return None

        captured: list[StoredToken | None] = []

        def _capture(settings: RemoteBuildSettings) -> None:
            captured.append(_bind(settings))

        # ``_modify_settings`` either runs the mutator exactly
        # once and returns, or it raises (which propagates).
        # Either way ``captured`` has length 1 on the success
        # path; ``[0]`` is safe.
        await self._modify_settings(_capture)
        return captured[0]

    @api_command("remote_build/set_settings")
    async def set_settings(self, *, enabled: bool, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Persist the receiver-side ``enabled`` master switch.

        Read-modify-write so manual hosts, tokens, and any future
        phase-3+ fields stay intact; a client toggling just
        ``enabled`` doesn't reset every other field to its default.

        Validates ``enabled`` is strictly a ``bool`` rather than
        coercing truthiness; a client sending the string ``"false"``
        for example would otherwise persist as ``True``, which is
        the opposite of what the user intended on a security-
        sensitive toggle.

        **Listener bind requires restart.** The HTTPS receiver
        site (``/remote-build/v1/*``) is bound once in
        :meth:`DeviceBuilder.start` based on the value at startup;
        flipping ``enabled`` here persists the new value but does
        NOT live-rebind. The frontend should surface a "restart
        required" hint to the operator. A future PR can wire
        ``set_settings`` into the lifecycle hooks if interactive
        toggling becomes a real UX concern.
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
    # Token CRUD (phase 3b1)
    # ------------------------------------------------------------------

    @api_command("remote_build/list_tokens")
    async def list_tokens(self, **kwargs: Any) -> list[TokenSummary]:
        """
        Return every issued bearer token, by ``TokenSummary``.

        ``TokenSummary`` rows never carry the secret hash; the
        cleartext bearer is generated client-side at
        ``add_token`` time and never crosses the wire to the
        backend, so this list intentionally has no path to
        recover it. The frontend renders the token_id + label +
        bound dashboard_id (if any) so the operator can audit
        which peers are paired.
        """
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        return [_summarise_token(token) for token in settings.tokens]

    @api_command("remote_build/add_token")
    async def add_token(
        self, *, label: str, token_id: str, secret_sha256: str, **kwargs: Any
    ) -> TokenSummary:
        """
        Register a client-generated bearer token under *label*.

        The CLIENT generates ``token_id`` + the cleartext secret,
        computes ``SHA-256(secret)`` locally, and submits
        ``{label, token_id, secret_sha256}``. The cleartext bearer
        never crosses the wire to the backend; only the hash is
        persisted. The frontend keeps the cleartext on screen
        long enough for the user to copy it into the offloader,
        then discards it.

        This wire shape closes the leak that would otherwise
        occur on plain-HTTP standalone deployments: the bearer
        is never present in any backend log, response, or stored
        request body. ``list_tokens`` returns
        :class:`TokenSummary` rows that don't carry the hash;
        if the user loses the cleartext, the only recovery is
        to remove the token and register a fresh one.

        Duplicate labels are allowed (``token_id`` is the unique
        key); a user may legitimately want two tokens both
        labelled "Green". Duplicate ``token_id`` is rejected
        with ``ALREADY_EXISTS`` — the client should retry with a
        freshly-generated id (collision is improbable at 64
        bits but not impossible at the soft cap).
        """
        clean_label = _validate_label(label)
        clean_token_id = _validate_token_id(token_id)
        clean_secret_sha256 = _validate_secret_sha256(secret_sha256)
        created_at = time.time()

        def _add(settings: RemoteBuildSettings) -> None:
            if len(settings.tokens) >= _MAX_TOKENS:
                msg = (
                    f"token list at capacity ({_MAX_TOKENS}); "
                    "remove an unused token before issuing a new one"
                )
                raise CommandError(ErrorCode.INVALID_ARGS, msg)
            for existing in settings.tokens:
                if existing.token_id == clean_token_id:
                    # Don't echo the token_id (already in the
                    # caller's hand; not a credential, but no
                    # need to mirror it back through error logs).
                    msg = "token_id collides with an existing token; retry with a fresh id"
                    raise CommandError(ErrorCode.ALREADY_EXISTS, msg)
            settings.tokens.append(
                StoredToken(
                    token_id=clean_token_id,
                    label=clean_label,
                    secret_sha256=clean_secret_sha256,
                    created_at=created_at,
                )
            )

        await self._modify_settings(_add)
        return TokenSummary(
            token_id=clean_token_id,
            label=clean_label,
            created_at=created_at,
        )

    @api_command("remote_build/remove_token")
    async def remove_token(self, *, token_id: str, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Revoke a previously-issued token.

        Removing a bound token immediately disconnects the
        offloader it's paired to: the next request the offloader
        sends presents a token_id the receiver no longer
        recognises and gets a 401. A non-existent ``token_id``
        raises ``NOT_FOUND`` so the caller knows the call was a
        no-op.
        """
        clean_id = _validate_token_id(token_id)

        def _remove(settings: RemoteBuildSettings) -> None:
            kept = [token for token in settings.tokens if token.token_id != clean_id]
            if len(kept) == len(settings.tokens):
                # Don't echo ``clean_id`` (or any user-supplied
                # input) here: validation rejects bearers up
                # front, but the principle is to keep credential-
                # adjacent input out of error messages by default.
                msg = "token is not registered"
                raise CommandError(ErrorCode.NOT_FOUND, msg)
            settings.tokens = kept

        return await self._modify_settings(_remove)

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
        ``/remote-build/v1/*`` HTTPS site is currently serving
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
            for peer in settings.peers:
                if peer.dashboard_id == clean_id:
                    if peer.status == PeerStatus.APPROVED:
                        msg = f"peer is already approved: {clean_id}"
                        raise CommandError(ErrorCode.INVALID_ARGS, msg)
                    peer.status = PeerStatus.APPROVED
                    return
            msg = f"no peer with dashboard_id: {clean_id}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)

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
            for peer in settings.peers:
                if peer.dashboard_id == clean_id:
                    previous_status = peer.status
                    break
            kept = [peer for peer in settings.peers if peer.dashboard_id != clean_id]
            if len(kept) == len(settings.peers):
                msg = f"no peer with dashboard_id: {clean_id}"
                raise CommandError(ErrorCode.NOT_FOUND, msg)
            settings.peers = kept

        view = await self._modify_settings(_remove)
        if previous_status == PeerStatus.APPROVED:
            self._fire_pair_status_changed(clean_id, "removed")
        return view

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

    def _fire_pair_status_changed(self, dashboard_id: str, status: str) -> None:
        """
        Fire ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` for a peer transition.

        ``status`` is ``"approved"`` (from :meth:`approve_peer`) or
        ``"removed"`` (from :meth:`remove_peer` of a previously-
        APPROVED row). Mirrors :meth:`_fire_pairing_window_changed`
        for shape; both methods are the named-intent boundary
        between controller logic and the bus payload format.
        """
        self._db.bus.fire(
            EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED,
            {"dashboard_id": dashboard_id, "status": status},
        )

    def _fire_pairing_window_changed(self) -> None:
        """Fire ``REMOTE_BUILD_PAIRING_WINDOW_CHANGED`` with the current state."""
        state = self._pairing_window_state()
        self._db.bus.fire(
            EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED,
            {"open": state.open, "expires_in_seconds": state.expires_in_seconds},
        )

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
