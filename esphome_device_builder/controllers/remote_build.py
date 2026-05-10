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
from enum import StrEnum
from pathlib import Path
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
from ..helpers.event_bus import Event
from ..helpers.json import dumps as json_dumps
from ..helpers.json import loads as json_loads
from ..helpers.peer_link_identity import get_or_create_peer_link_identity
from ..helpers.storage import ShutdownCallback, Store
from ..models import (
    ErrorCode,
    EventType,
    IdentityView,
    IntentResponse,
    OffloaderPairStatusChangedData,
    OffloaderRemoteBuildSettings,
    PairingSummary,
    PairingWindowState,
    PeerStatus,
    PeerSummary,
    ReceiverPeers,
    RemoteBuildHostRemovedData,
    RemoteBuildIdentityRotatedData,
    RemoteBuildPairingWindowChangedData,
    RemoteBuildPairRequestReceivedData,
    RemoteBuildPairStatusChangedData,
    RemoteBuildPeer,
    RemoteBuildPeerSource,
    RemoteBuildSettings,
    RemoteBuildSettingsView,
    StoredPairing,
    StoredPeer,
)
from .config import (
    load_remote_build_settings,
    remote_build_settings_transaction,
)
from .remote_build_peer_link import PeerLinkSession, TerminateReason
from .remote_build_peer_link_client import (
    PairStatusResult,
    PeerLinkClientError,
)
from .remote_build_peer_link_client import (
    await_pair_status as peer_link_await_pair_status,
)
from .remote_build_peer_link_client import (
    preview_pair as peer_link_preview_pair,
)
from .remote_build_peer_link_client import (
    request_pair as peer_link_request_pair,
)

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder
    from ..helpers.dashboard_identity import DashboardIdentity
    from ..helpers.peer_link_identity import PeerLinkIdentity

_LOGGER = logging.getLogger(__name__)


def _load_offloader_identities(
    config_dir: Path,
) -> tuple[PeerLinkIdentity, DashboardIdentity]:
    """Load both offloader-side identities in one executor hop.

    The peer-link X25519 keypair drives the Noise XX handshake;
    the dashboard cert (phase 3a) carries the stable
    ``dashboard_id`` we send in msg3 so the receiver's
    ``StoredPeer`` row keys on it. The two are both lazy-create
    on first read, both protected by per-process locks
    in their respective helpers, and both involve disk I/O
    (each is one file read + occasional first-call generation).
    Bundling into a single sync helper means one
    ``run_in_executor`` round-trip rather than two — matters
    less for the threadpool overhead than for the
    "two awaits where one would do" code shape; keeps the
    caller's body tight.
    """
    return get_or_create_peer_link_identity(config_dir), get_or_create_identity(config_dir)


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


_HOSTNAME_MAX_CHARS = 255  # RFC 1035 §2.3.4 caps a FQDN at 253; round up to 255.


class _HostFieldContext(StrEnum):
    """Error-message prefix for the shared host / port validators.

    The same ``_validate_hostname`` / ``_validate_port`` pair
    gates the offloader-side ``preview_pair`` / ``request_pair``
    flow and any future receiver-side host-input surface.
    Hardcoding a single prefix in the error messages would leak
    misleading diagnostics into the WS layer; pick the right
    prefix at the call site instead.

    StrEnum values are the message prefix verbatim; new call sites
    that want a distinct user-facing string add a new enum member
    rather than passing a free-form string (so the prefixes are
    grep-able and don't drift).
    """

    RECEIVER = "receiver"


class _PairLabelField(StrEnum):
    """Wire arg name for ``_validate_pair_label`` error messages.

    ``request_pair`` takes two distinct labels — ``receiver_label``
    for local storage and ``offloader_label`` sent to the receiver
    in msg3 — and a validation failure must name the failing arg so
    the frontend can pin the inline error to the right input. StrEnum
    values are the wire arg name verbatim; new call sites add a new
    enum member rather than passing a free-form string (mirrors
    :class:`_HostFieldContext`).
    """

    RECEIVER_LABEL = "receiver_label"
    OFFLOADER_LABEL = "offloader_label"


def _validate_hostname(
    raw: object, *, context: _HostFieldContext = _HostFieldContext.RECEIVER
) -> str:
    """
    Normalise a user-entered hostname to its canonical lowercase form.

    Rejects non-string and empty / whitespace-only input with
    :class:`CommandError(INVALID_ARGS)`. Caps length at
    :data:`_HOSTNAME_MAX_CHARS` (RFC 1035 §2.3.4 caps a fully-
    qualified domain name at 253 characters; we accept up to 255
    to leave room for trailing-dot variations). The cap stops a
    misbehaving frontend from bloating the on-disk pairings file
    (and, for the offloader-side pairing pool, the
    ``initial_state`` snapshot served on every
    ``subscribe_events`` subscription) with a megabyte-string
    masquerading as a hostname.

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
        msg = f"{context}: 'hostname' must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    trimmed = raw.strip().lower()
    if not trimmed:
        msg = f"{context}: 'hostname' must not be empty"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if len(trimmed) > _HOSTNAME_MAX_CHARS:
        msg = f"{context}: 'hostname' must be at most {_HOSTNAME_MAX_CHARS} characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    # The ``port=80, path="/"`` are sentinels for the build call
    # — only the host arg is being validated. yarl's host parser
    # is the same one ``_build_ws_url`` will use later, so any
    # input that passes here is guaranteed to round-trip
    # through the URL builder without raising.
    try:
        URL.build(scheme="ws", host=trimmed, port=80, path="/")
    except ValueError as exc:
        msg = f"{context}: 'hostname' is not a valid host: {exc}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg) from exc
    return trimmed


def _peer_summary(peer: StoredPeer, *, status: PeerStatus) -> PeerSummary:
    """Project a :class:`StoredPeer` to wire :class:`PeerSummary`.

    Drops the raw ``static_x25519_pub`` bytes; ``pin_sha256`` is
    the wire-friendly form UIs render for OOB-verification, and
    the pubkey is only needed server-side to look up the peer
    against an incoming Noise handshake. ``status`` is supplied
    by the caller because :class:`StoredPeer` itself doesn't
    carry one — pending peers live in the controller's in-memory
    dict and persisted peers are implicitly approved.
    """
    return PeerSummary(
        dashboard_id=peer.dashboard_id,
        pin_sha256=peer.pin_sha256,
        label=peer.label,
        paired_at=peer.paired_at,
        status=status,
        peer_ip=peer.peer_ip,
    )


def _pairing_summary(pairing: StoredPairing) -> PairingSummary:
    """Project a :class:`StoredPairing` to wire :class:`PairingSummary`.

    Mirror of :func:`_peer_summary` for the offloader side. Drops
    the raw ``static_x25519_pub`` bytes. ``status`` reads off the
    row — the unified in-RAM ``_pairings`` dict carries both
    PENDING and APPROVED rows, with the disk filter stripping
    PENDING at serialise time.
    """
    return PairingSummary(
        receiver_hostname=pairing.receiver_hostname,
        receiver_port=pairing.receiver_port,
        pin_sha256=pairing.pin_sha256,
        label=pairing.label,
        paired_at=pairing.paired_at,
        status=pairing.status,
    )


_PIN_SHA256_LEN = 64  # 32-byte SHA-256 → 64 lowercase-hex chars
_PAIR_LABEL_MAX_CHARS = 128  # mirrors :data:`_PEER_LABEL_MAX_CHARS` on the receiver


def _validate_pin_sha256(raw: object) -> str:
    """Validate a wire ``pin_sha256`` value as 64 lowercase-hex chars.

    Same alphabet and length the storage seam enforces in
    :class:`StoredPairing` (and the receiver's :class:`StoredPeer`),
    just at the WS-command boundary so a bad pin gets rejected as
    ``INVALID_ARGS`` before the offloader opens a Noise WS only to
    fail the TOCTOU check post-handshake.
    """
    if not isinstance(raw, str):
        msg = "pin_sha256 must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    cleaned = raw.strip()
    if (
        len(cleaned) != _PIN_SHA256_LEN
        or not cleaned.isascii()
        or any(c not in "0123456789abcdef" for c in cleaned)
    ):
        msg = f"pin_sha256 must be {_PIN_SHA256_LEN} lowercase-hex characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return cleaned


def _validate_pair_label(raw: object, *, field: _PairLabelField) -> str:
    """Validate a user-supplied pair-flow label.

    Capped at 128 chars to match
    :data:`controllers.remote_build_peer_link._PEER_LABEL_MAX_CHARS`
    (the receiver's truncation cap on the same field), so a
    label that round-trips through pair_request lands in both
    sides' tables with identical content. Empty labels are
    allowed; the user may legitimately not name the receiver
    yet, and the frontend can render a placeholder.

    Rejects strings containing C0 / C1 control chars (incl. null
    bytes, ANSI escapes, newlines, DEL) via :meth:`str.isprintable`.
    The ``offloader_label`` transits to the receiver-side admin
    UI's pairing-requests inbox; refusing control chars here is
    defense-in-depth against ANSI / bidi-override / null-byte
    injection attacks against an admin terminal or log reader
    (the load-bearing fix is on the receiver side, but symmetric
    rejection here catches honest typos and a future
    direct-driver caller that bypasses the receiver's normaliser).
    Non-ASCII printables (CJK, accented Latin, emoji) pass —
    :meth:`str.isprintable` only excludes the C0/C1 control sets
    plus surrogates, which is the right cut for a name field.

    *field* names the failing arg in the diagnostic via
    :class:`_PairLabelField` so the frontend can pin the inline
    error to the right input.
    """
    if not isinstance(raw, str):
        msg = f"{field} must be a string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    cleaned = raw.strip()
    if len(cleaned) > _PAIR_LABEL_MAX_CHARS:
        msg = f"{field} must be at most {_PAIR_LABEL_MAX_CHARS} characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if not cleaned.isprintable():
        msg = f"{field} must contain only printable characters"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return cleaned


# Maps non-success ``IntentResponse`` values from a peer-link
# round-trip to the typed :class:`CommandError` the frontend
# branches on. Used by ``request_pair`` to surface the receiver's
# decision (the offloader-side pair-status listener task handles
# its own ``IntentResponse`` branches inline rather than going
# through CommandError, so this map only covers the WS-command
# request_pair path).
_INTENT_RESPONSE_ERRORS: dict[IntentResponse, tuple[ErrorCode, str]] = {
    IntentResponse.NO_PAIRING_WINDOW: (
        ErrorCode.NO_PAIRING_WINDOW,
        "receiver pairing window closed; ask the receiver-side admin to "
        "open Settings → Build server → Pairing requests, then retry",
    ),
    IntentResponse.REJECTED: (
        ErrorCode.PRECONDITION_FAILED,
        "receiver declined the pair request",
    ),
}


def _intent_response_to_command_error(status: IntentResponse) -> CommandError | None:
    """Translate a non-success ``IntentResponse`` to a typed ``CommandError``.

    Returns ``None`` for the success values
    (``OK``, ``PENDING``, ``APPROVED``); the caller branches on
    those for persistence. Returns a fresh ``CommandError`` (not
    yet raised) for ``REJECTED`` / ``NO_PAIRING_WINDOW`` so the
    caller can decide whether to attach extra context before
    raising.
    """
    pair = _INTENT_RESPONSE_ERRORS.get(status)
    if pair is None:
        return None
    code, msg = pair
    return CommandError(code, msg)


def _enforce_pin_match(*, expected: str, observed: str) -> None:
    """Raise ``CommandError(PRECONDITION_FAILED)`` on a TOCTOU pin drift.

    The offloader's ``request_pair`` (and any future
    pin-pinned re-handshake) compares the pin the user
    OOB-confirmed during ``preview_pair`` against the actual
    pubkey from the live handshake. A mismatch means the
    receiver rotated identity (or a MITM intervened) between
    preview and request; the offloader bails before persisting
    the row so a fresh preview round-trip is required.

    The error message carries both pins in full (no
    truncation) so the user can do a side-by-side OOB
    comparison against the receiver's "Build server"
    Settings card and tell which end's pin changed.
    Truncating the displayed pin would shrink the log volume
    but at the cost of letting an attacker who deliberately
    collides a 16-char prefix slip the mismatch past a
    quick visual scan; the human OOB check is the
    load-bearing security property, so full digest wins.
    """
    # Plain ``==`` is fine here: the pin is a SHA-256 of a public
    # key, broadcast in mDNS and rendered in the receiver's UI.
    # There's no secret to leak via timing; constant-time
    # comparison would be defending nothing.
    if expected == observed:
        return
    msg = f"receiver pin changed since preview; expected {expected}, got {observed}"
    raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)


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


def _validate_port(raw: object, *, context: _HostFieldContext = _HostFieldContext.RECEIVER) -> int:
    """
    Validate a user-entered port number.

    ``bool`` is rejected even though ``isinstance(True, int)`` is
    true; accepting ``True`` for a port number is a footgun
    (silently coerces to 1, which IANA reserves for tcpmux).
    Range is the IANA-registered ephemeral plus
    well-known: 1-65535.

    *context* prefixes every error message; see
    :class:`_HostFieldContext` for the rationale and the
    list of valid prefixes.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        msg = f"{context}: 'port' must be an integer"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if not 1 <= raw <= 65535:
        msg = f"{context}: 'port' must be between 1 and 65535"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return raw


# Sleep before reconnecting a pair-status listener whose Noise WS
# died on transport error (TCP RST, receiver bounce, transient
# blip). Bounds tight-looping against a hard-down receiver. Two
# seconds is short enough that a recoverable blip recovers fast
# and long enough that a wedged receiver doesn't burn CPU.
_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS = 2.0

# Debounce window for the offloader-side pairings store write.
# Pair / unpair / approve flips happen in bursts (admin Accepts a
# whole inbox of pending pairings, the listener tasks fire near-
# simultaneously); a one-second window collapses those into one
# disk write without making any single mutation visible externally
# before it lands. Picked to roughly match HA's typical
# ``async_delay_save`` cadence on its own ``Store`` callers.
_PAIRINGS_SAVE_DELAY_SECONDS = 1.0

# On-disk filename for the offloader-side pairings store. Sibling
# of ``.device-builder.json`` in ``config_dir`` rather than a
# sub-key of it: per-domain atomicity, no lock contention against
# unrelated writers, and matches HA's per-file ``Store`` shape.
# Leading dot keeps the file out of normal directory listings on
# the user's editor pane (same convention as ``.device-builder.json``).
_OFFLOADER_PAIRINGS_FILE = ".offloader_pairings.json"
_RECEIVER_PEERS_FILE = ".receiver_peers.json"


def _encode_pairings(value: OffloaderRemoteBuildSettings) -> bytes:
    """Serialise the offloader-side pairings shape for the store."""
    return json_dumps(value.to_dict())


def _decode_pairings(raw: bytes) -> OffloaderRemoteBuildSettings:
    """Decode the offloader-side pairings shape from the store.

    Defaults on a malformed blob rather than crashing dashboard
    startup. The ``Store`` lets decoder errors propagate so a
    consumer can pick the recovery posture; here we want
    "soft-recover to empty" because a corrupt pairings file means
    every offloader has to re-pair (annoying) but isn't fatal,
    whereas crashing the dashboard would lock the user out
    entirely.
    """
    try:
        return OffloaderRemoteBuildSettings.from_dict(json_loads(raw))
    except Exception:
        _LOGGER.exception("Corrupt offloader pairings file; resetting to empty")
        return OffloaderRemoteBuildSettings()


def _encode_peers(value: ReceiverPeers) -> bytes:
    """Serialise the receiver-side peers shape for the store."""
    return json_dumps(value.to_dict())


def _decode_peers(raw: bytes) -> ReceiverPeers:
    """Decode the receiver-side peers shape from the store.

    Soft-recover to empty on malformed blobs, mirror of
    :func:`_decode_pairings`. A corrupt peers file means every
    paired offloader has to re-pair — annoying, not fatal — so
    crashing dashboard startup is the wrong recovery posture.
    """
    try:
        return ReceiverPeers.from_dict(json_loads(raw))
    except Exception:
        _LOGGER.exception("Corrupt receiver peers file; resetting to empty")
        return ReceiverPeers()


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
        # One long-running asyncio.Task per PENDING
        # :class:`StoredPairing`, spawned by ``request_pair`` when
        # it lands a row in ``_pairings`` and cancelled
        # by ``unpair`` / a re-pair against the same address /
        # the listener's own terminal-flip exit. The task holds
        # an open Noise WS to its receiver with
        # ``intent="pair_status"``; the receiver-side responder
        # parks on its bus event for an admin-click and pushes
        # the response back, so the offloader sees the flip with
        # sub-second latency without polling.
        #
        # No cold-start spawn: PENDING is in-memory only, so a
        # controller restart starts with an empty dict and there's
        # nothing to rebuild from disk. The bus event the
        # listener fires on flip
        # (:attr:`EventType.OFFLOADER_PAIR_STATUS_CHANGED`) is
        # picked up by any client subscribed to the global
        # ``subscribe_events`` stream — no separate
        # ``subscribe_pairings`` channel needed.
        #
        # Keyed on ``(receiver_hostname, receiver_port)`` to match
        # the unified ``_pairings`` dict.
        self._pair_status_listeners: dict[tuple[str, int], asyncio.Task[None]] = {}
        # PENDING StoredPeer rows live here, keyed on dashboard_id.
        # Never persisted — the per-file ``_peers_store``
        # (``.receiver_peers.json``) only stores APPROVED rows.
        # Bounded lifetime: rows land via ``record_pair_request``
        # while the pairing window is open, and the dict is
        # cleared on window auto-close so a malicious LAN scanner
        # can't fill the receiver's persistent state with junk
        # pair-requests. Cleared rows fire
        # ``REMOTE_BUILD_PAIR_STATUS_CHANGED("removed")`` (the
        # *receiver-side* bus event — distinct from the
        # offloader-side ``OFFLOADER_PAIR_STATUS_CHANGED`` event
        # that the offloader's listener task fires after observing
        # the pair_status response) so any offloader currently
        # long-polling pair_status sees the cancellation. On
        # controller restart this dict is empty — any in-flight
        # pair attempts have to be re-initiated by the offloader.
        self._pending_peers: dict[str, StoredPeer] = {}
        # RAM-canonical APPROVED peers, keyed on
        # ``dashboard_id``. Loaded once at :meth:`start` from the
        # per-file ``_peers_store`` (sibling-of-sidecar
        # ``.receiver_peers.json``) and mutated immediately by
        # ``approve_peer`` / ``remove_peer`` — disk persistence
        # rides a debounced ``async_delay_save``, so the in-RAM
        # update doesn't block on the write and a save failure
        # doesn't roll back the user-visible mutation. Reads
        # (snapshot, ``_to_view``, ``_lookup_peer_response``)
        # short-circuit through this dict so no read path hits
        # disk while a write is in flight — the disk-read-vs-write
        # race a fresh ``load_remote_build_settings`` on every read
        # would expose is closed structurally. Cleared in
        # :meth:`stop` for shutdown ordering.
        self._approved_peers: dict[str, StoredPeer] = {}
        # Active long-lived peer-link sessions, keyed on the
        # offloader's ``dashboard_id``. Populated by
        # :meth:`register_peer_link_session` after a successful
        # ``intent="peer_link"`` Noise handshake (5a-1) and
        # cleared on session exit (peer close / heartbeat
        # timeout / shutdown). One entry per dashboard_id —
        # a duplicate connect kicks the older session via
        # ``TerminateReason.SUPERSEDED`` so a restarted
        # offloader takes over its previous slot rather than
        # doubling. Drained in :meth:`stop`.
        self._peer_link_sessions: dict[str, PeerLinkSession] = {}
        # Single offloader-side ``StoredPairing`` map: contains both
        # PENDING and APPROVED rows, keyed on
        # ``(receiver_hostname, receiver_port)``. Source of truth at
        # runtime; the disk filter at serialise time strips PENDING
        # rows so the on-disk shape stays APPROVED-only. Loaded once
        # at :meth:`start` and mutated in-place on every
        # ``request_pair`` / ``unpair`` /
        # ``_apply_pair_status_result`` — saves debounce through
        # :attr:`_pairings_store`.
        self._pairings: dict[tuple[str, int], StoredPairing] = {}
        # ``Store`` registers itself with this list at construction
        # (via ``shutdown_register=...append``); the controller's
        # :meth:`stop` walks the list to flush any debounced save
        # before shutdown returns. Living on the controller rather
        # than ``DeviceBuilder`` keeps the lifecycle layer scoped to
        # the same object that owns the state.
        # ``DeviceBuilder.stop`` already awaits :meth:`stop`, so the
        # chain is unbroken.
        self._shutdown_callbacks: list[ShutdownCallback] = []
        self._pairings_store: Store[OffloaderRemoteBuildSettings] = Store(
            self._db.settings.config_dir / _OFFLOADER_PAIRINGS_FILE,
            encoder=_encode_pairings,
            decoder=_decode_pairings,
            shutdown_register=self._shutdown_callbacks.append,
            name="offloader_pairings",
        )
        # Receiver-side APPROVED peers, persisted via the same
        # per-file ``Store`` shape the offloader uses for
        # pairings. RAM is canonical at runtime; disk is just
        # persistence across restarts. Mutations land via
        # ``async_delay_save`` so a burst of ``approve_peer`` /
        # ``remove_peer`` collapses into one disk write.
        self._peers_store: Store[ReceiverPeers] = Store(
            self._db.settings.config_dir / _RECEIVER_PEERS_FILE,
            encoder=_encode_peers,
            decoder=_decode_peers,
            shutdown_register=self._shutdown_callbacks.append,
            name="receiver_peers",
        )

    async def start(self) -> None:
        """
        Wire the browser onto the shared zeroconf and capture self-name.

        Also seeds :attr:`_pairings` from the offloader-side
        per-file store so a restart doesn't lose previously-APPROVED
        rows. The load runs unconditionally (even when zeroconf is
        down — APPROVED pairings are offloader-side state
        independent of mDNS); the rest of the start path bails when
        the shared zeroconf isn't up. Peer discovery stays
        fail-soft; same contract as
        :class:`DashboardAdvertiser`.
        """
        # Load APPROVED pairings into RAM. ``StoredPairing.status``
        # defaults to ``APPROVED`` so older sidecars without the
        # field round-trip cleanly; freshly-saved files carry the
        # explicit ``status="approved"`` (PENDING rows are filtered
        # out at serialise time, never on disk). The store hops to
        # the executor so this read doesn't block startup. ``None``
        # means the file doesn't exist yet (fresh install) — the
        # dict stays empty.
        if (settings := await self._pairings_store.async_load()) is not None:
            for pairing in settings.pairings:
                self._pairings[(pairing.receiver_hostname, pairing.receiver_port)] = pairing
        # Seed the RAM-canonical APPROVED peer dict from the
        # per-file peers store. Mirrors the offloader-side
        # ``_pairings_store`` load above; RAM is canonical from
        # this point on, every read short-circuits through
        # ``_approved_peers`` and every mutation schedules a
        # debounced write.
        if (peers_state := await self._peers_store.async_load()) is not None:
            for peer in peers_state.peers:
                self._approved_peers[peer.dashboard_id] = peer
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

    async def register_peer_link_session(self, session: PeerLinkSession) -> None:
        """
        Register *session* in the active peer-link registry.

        If a session already exists for the same ``dashboard_id``,
        it's evicted via :class:`TerminateReason.SUPERSEDED` —
        a restarted offloader takes over its previous slot
        rather than doubling. The eviction's ``terminate`` frame
        is best-effort: a peer that has already gone away won't
        receive it, but the WS close still drains the old
        session's receive loop and unregistration runs in its
        ``finally``. The new session installs into the registry
        synchronously *before* this awaitable suspends so a
        subsequent dispatch lookup sees the freshest entry.
        """
        existing = self._peer_link_sessions.get(session.dashboard_id)
        # Install the new session first so a concurrent dispatch
        # sees it; the old session's terminate is the slow
        # awaitable path.
        self._peer_link_sessions[session.dashboard_id] = session
        if existing is not None and existing is not session:
            await existing.terminate(TerminateReason.SUPERSEDED)

    def unregister_peer_link_session(self, session: PeerLinkSession) -> None:
        """
        Drop *session* from the active peer-link registry.

        No-op when a different session has taken the slot (the
        :meth:`register_peer_link_session` dedupe path replaces
        the entry before the old session's loop unwinds; the old
        loop's ``finally`` calls this and would otherwise evict
        the new entry). Sync because it's just a dict pop — the
        actual WS close + Noise teardown happens in the session
        loop's ``finally`` chain.
        """
        if self._peer_link_sessions.get(session.dashboard_id) is session:
            del self._peer_link_sessions[session.dashboard_id]

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
        # Cancel + drain offloader-side pair-status listener tasks
        # so they don't leak past controller shutdown. Each
        # listener self-removes from ``_pair_status_listeners``
        # via its ``finally`` clause; the dict-clear at the end
        # is belt-and-braces in case a task crashed before
        # reaching its finally.
        for task in self._pair_status_listeners.values():
            task.cancel()
        if self._pair_status_listeners:
            await asyncio.gather(*self._pair_status_listeners.values(), return_exceptions=True)
            self._pair_status_listeners.clear()
        if self._pairing_window_handle is not None:
            self._pairing_window_handle.cancel()
            self._pairing_window_handle = None
        self._pairing_window_clients.clear()
        # Drain every active peer-link session before the rest
        # of the controller-state cleanup runs. ``terminate``
        # sends a structured ``terminate{reason: server_shutting_down}``
        # frame and closes the WS; the session's loop unwinds via
        # ``unregister_peer_link_session`` in its ``finally``,
        # which mutates ``self._peer_link_sessions`` — snapshot
        # to a list first so the iteration doesn't race the dict
        # mutation. Each terminate is best-effort (a peer that
        # has already gone away just gets the close).
        for peer_link_session in list(self._peer_link_sessions.values()):
            await peer_link_session.terminate(TerminateReason.SERVER_SHUTTING_DOWN)
        self._peer_link_sessions.clear()
        # Route the receiver-side PENDING clear through the same
        # helper the auto-close + explicit-close paths use, so
        # any in-flight pair_status long-poll on a still-alive bus
        # (a future "soft reload" path that tears down the
        # controller without closing the dashboard's WS) sees
        # the same removal events as window-close. At
        # process-shutdown the bus has no listeners anyway, so
        # the events are absorbed cheaply.
        self._clear_pending_peers_on_window_close()
        # Flush any debounced disk saves before the dict goes away.
        # ``_shutdown_callbacks`` was populated by every ``Store`` we
        # constructed (currently just the pairings store; future
        # offloader-side stores can register the same way). Walk in
        # registration order so a future cross-store dependency
        # lands deterministically.
        for callback in self._shutdown_callbacks:
            await callback()
        # Offloader-side pairings dict has no bus-event semantic on
        # clear (it's the offloader's local UI state, not a
        # receiver-visible row), so silent clear is fine here.
        self._pairings.clear()
        self._peers.clear()
        # Receiver-side APPROVED peers clear silently too —
        # unlike :meth:`_clear_pending_peers_on_window_close`
        # which fires per-row ``status="removed"`` because PENDING
        # rows are in-flight pairing state that long-pollers are
        # actively watching, APPROVED rows are persistent trust
        # anchors. A subscriber observing the dashboard come back
        # up after a restart sees them populate from the next
        # ``subscribe_events`` initial_state. Symmetric with the
        # offloader-side ``_pairings.clear()`` above.
        self._approved_peers.clear()

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

        Filters our own service-instance name so we don't surface
        our own advertise as a discovered host. ``Removed`` events
        delete the peer immediately and fire
        :attr:`EventType.REMOTE_BUILD_HOST_REMOVED`; ``Added`` /
        ``Updated`` resolve either from the zeroconf cache (sync,
        fires :attr:`EventType.REMOTE_BUILD_HOST_ADDED` inline) or
        via a fire-and-forget task (async, fires from
        :meth:`_resolve_and_apply` once the SRV / TXT round-trip
        completes).
        """
        if name == self._own_instance_name:
            return
        if state_change == ServiceStateChange.Removed:
            popped = self._peers.pop(name, None)
            if popped is not None:
                # Event keys on the wire-friendly ``peer.name``
                # (leftmost label) so frontend dicts keyed on the
                # ``RemoteBuildPeer.name`` field upsert/delete
                # consistently. The FQDN ``name`` is the
                # ``self._peers`` dict key only.
                self._fire_host_removed(popped.name)
            return
        info = AsyncServiceInfo(service_type, name)
        if info.load_from_cache(zeroconf):
            self._upsert_host(name, info)
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
        self._upsert_host(name, info)

    def _upsert_host(self, name: str, info: AsyncServiceInfo) -> None:
        """Replace the row keyed on *name* and fire ``REMOTE_BUILD_HOST_ADDED``.

        Called from both the cache-hit and resolve-success paths;
        centralises the dict-mutation + event-fire so a future
        TXT-only refresh doesn't accidentally skip the event. The
        event payload is the same :meth:`RemoteBuildPeer.to_dict`
        projection :meth:`hosts_snapshot` delivers in the
        ``subscribe_events`` initial-state push, so a snapshot-
        loaded row and a live-event row carry identical fields by
        construction; adding a field to :class:`RemoteBuildPeer`
        flows through both surfaces in lockstep without a manual
        bookkeeping update here.
        """
        peer = _peer_from_service_info(name, info)
        self._peers[name] = peer
        self._db.bus.fire(EventType.REMOTE_BUILD_HOST_ADDED, peer.to_dict())

    def _fire_host_removed(self, name: str) -> None:
        """Fire ``REMOTE_BUILD_HOST_REMOVED`` for *name*."""
        payload: RemoteBuildHostRemovedData = {"name": name}
        self._db.bus.fire(EventType.REMOTE_BUILD_HOST_REMOVED, payload)

    def hosts_snapshot(self) -> list[RemoteBuildPeer]:
        """Return the current mDNS-discovered hosts.

        Pure synchronous read of ``self._peers`` — no executor
        hop, no disk read. Mirrors ``pairings_snapshot`` /
        ``peers_snapshot``: the snapshot seeds the
        ``subscribe_events`` initial-state push so a fresh tab
        paints without a round-trip; live updates flow from
        :attr:`EventType.REMOTE_BUILD_HOST_ADDED` /
        :attr:`EventType.REMOTE_BUILD_HOST_REMOVED` events fired
        by the mDNS callbacks above.
        """
        return list(self._peers.values())

    # ------------------------------------------------------------------
    # API surface
    # ------------------------------------------------------------------

    @api_command("remote_build/get_settings")
    async def get_settings(self, **kwargs: Any) -> RemoteBuildSettingsView:
        """Return the receiver-side remote-build settings (wire view)."""
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        return self._to_view(settings)

    def _to_view(self, settings: RemoteBuildSettings) -> RemoteBuildSettingsView:
        """Project receiver settings to wire view, merging in-memory peers.

        The peer list is RAM-canonical: PENDING entries live in
        ``self._pending_peers`` for the active pairing window's
        lifetime (never hit disk) and APPROVED entries live in
        ``self._approved_peers`` / its per-file ``Store``.
        ``settings`` is consulted for the master ``enabled``
        toggle.
        """
        return RemoteBuildSettingsView(
            enabled=settings.enabled,
            peers=self._peer_summaries(),
        )

    def _peer_summaries(self) -> list[PeerSummary]:
        """Merge PENDING + APPROVED into a single ``PeerSummary`` list."""
        return [
            _peer_summary(p, status=PeerStatus.PENDING) for p in self._pending_peers.values()
        ] + [_peer_summary(p, status=PeerStatus.APPROVED) for p in self._approved_peers.values()]

    def peers_snapshot(self) -> list[PeerSummary]:
        """
        Return the in-memory peers snapshot (PENDING + APPROVED).

        Pure synchronous read of ``_pending_peers`` +
        ``_approved_peers`` — no executor hop, no disk read, no
        race window. :meth:`start` seeded ``_approved_peers``
        from disk; every mutation since has flowed through the
        same dict, so RAM is the canonical source of truth (mirrors
        the offloader-side :meth:`pairings_snapshot` shape).

        Used by
        :meth:`device_builder.DeviceBuilder._cmd_subscribe_events`
        to seed the frontend's initial state. Live updates flow
        from ``REMOTE_BUILD_PAIR_REQUEST_RECEIVED`` and
        ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` bus events through
        the same ``subscribe_events`` stream — the events fire
        right after the dict mutation, so a subscriber that reads
        the snapshot then attaches its event handler will see
        every state change without missing any.

        Not a WS command: the dashboard frontend always subscribes
        on app-startup, so a separate snapshot read would just be
        a redundant round-trip.
        """
        return self._peer_summaries()

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
        return self._to_view(settings)

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

        Live-rebinds the peer-link Noise WS listener after the
        write lands: a flip to ``True`` runs the same bind path
        :meth:`DeviceBuilder._maybe_start_remote_build_site` does
        at startup; a flip to ``False`` tears down the runner and
        clears the mDNS pin/port advertise. Fail-soft on bind error
        — the dashboard keeps running without a listener and a
        subsequent ``set_settings`` retry can clear a transient
        port conflict without a restart.
        """
        if not isinstance(enabled, bool):
            msg = "remote_build/set_settings: 'enabled' must be a boolean"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

        def _set(settings: RemoteBuildSettings) -> None:
            settings.enabled = enabled

        view = await self._modify_settings(_set)
        await self._db.apply_remote_build_enabled()
        return view

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
        clean_host = _validate_hostname(hostname, context=_HostFieldContext.RECEIVER)
        clean_port = _validate_port(port, context=_HostFieldContext.RECEIVER)
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

    @api_command("remote_build/request_pair")
    async def request_pair(
        self,
        *,
        hostname: str,
        port: int,
        pin_sha256: str,
        receiver_label: str,
        offloader_label: str,
        **kwargs: Any,
    ) -> PairingSummary:
        """Open a Noise XX WS, send ``intent="pair_request"``, persist a local row.

        The offloader's second handshake with a receiver, after
        the user has OOB-confirmed the receiver's pin via
        :meth:`preview_pair`. Sends ``{"label":
        offloader_label, "dashboard_id": <ours>}`` in the
        encrypted msg3 payload; the receiver's response decides
        what state the local :class:`StoredPairing` row lands
        in.

        Two distinct labels because the offloader-side and
        receiver-side rows mean different things:

        * *receiver_label* — what the offloader's user calls the
          receiver in their own settings UI (e.g. "desktop").
          Persisted to ``StoredPairing.label``; never sent to
          the receiver.
        * *offloader_label* — what the offloader-side user
          identifies *itself* as so the receiver-side admin's
          Pairing requests inbox shows a friendly name (e.g.
          "green-laptop"). Sent to the receiver in the
          encrypted msg3 payload; the receiver's
          ``record_pair_request`` lands it in
          ``StoredPeer.label``. Never persisted on the
          offloader side.

        TOCTOU defense: the *pin_sha256* arg is the value the
        user OOB-confirmed in preview; the live handshake
        captures the receiver's actual pubkey. If the two don't
        match — receiver rotated identity between preview and
        request, or an active MITM intervened — the call
        rejects with ``PRECONDITION_FAILED`` and persists
        nothing. The offloader's frontend should re-run
        preview before retrying.

        Args:
            hostname: Receiver's hostname (validated /
                normalised by :func:`_validate_hostname`;
                yarl-correct).
            port: Receiver's peer-link port (1-65535).
            pin_sha256: Lowercase-hex SHA-256 of the receiver's
                X25519 pubkey, captured + OOB-verified during
                ``preview_pair``.
            receiver_label: Offloader-side display name for the
                receiver (stored locally only).
            offloader_label: Offloader-side self-identification
                label (sent to the receiver, stored receiver-
                side).

        Returns:
            :class:`PairingSummary` for the newly-created or
            refreshed :class:`StoredPairing` row, with
            ``status`` = ``PENDING`` (typical) or ``APPROVED``
            (re-pair against a row the receiver already
            approved).

        Raises:
            :class:`CommandError(INVALID_ARGS)` for bad inputs
                (host / port / pin / label shape).
            :class:`CommandError(UNAVAILABLE)` for transport,
                handshake, or decode failures.
            :class:`CommandError(PRECONDITION_FAILED)` for
                pin mismatch (TOCTOU) or receiver-side
                ``REJECTED`` (admin declined).
            :class:`CommandError(NO_PAIRING_WINDOW)` when the
                receiver returns ``no_pairing_window`` —
                frontend prompts the user to ask the
                receiver-side admin to open the Pairing
                requests screen.

        Persistence note: only APPROVED rows are written to disk.
        PENDING rows live in the controller's in-memory
        ``_pairings`` dict for the lifetime of the
        offloader process. If the offloader is restarted while a
        request is still pending, the dict starts empty and the
        user has to re-issue ``request_pair``; this matches the
        receiver's symmetric "controller restart drops PENDING"
        property (the receiver-side window-close that triggered
        any in-flight pair_request would have cleared its own
        pending dict anyway, so cross-restart pending is never
        a coherent state on either side).
        """
        clean_host = _validate_hostname(hostname, context=_HostFieldContext.RECEIVER)
        clean_port = _validate_port(port, context=_HostFieldContext.RECEIVER)
        clean_pin = _validate_pin_sha256(pin_sha256)
        clean_receiver_label = _validate_pair_label(
            receiver_label, field=_PairLabelField.RECEIVER_LABEL
        )
        clean_offloader_label = _validate_pair_label(
            offloader_label, field=_PairLabelField.OFFLOADER_LABEL
        )
        loop = asyncio.get_running_loop()
        peer_link_identity, dashboard_identity = await loop.run_in_executor(
            None, _load_offloader_identities, self._db.settings.config_dir
        )

        try:
            result = await peer_link_request_pair(
                hostname=clean_host,
                port=clean_port,
                identity_priv=peer_link_identity.private_bytes,
                label=clean_offloader_label,
                dashboard_id=dashboard_identity.dashboard_id,
            )
        except PeerLinkClientError as exc:
            raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc

        _enforce_pin_match(expected=clean_pin, observed=result.pin_sha256)
        if (err := _intent_response_to_command_error(result.status)) is not None:
            raise err
        if result.status not in (IntentResponse.PENDING, IntentResponse.APPROVED):
            msg = f"unexpected receiver intent_response={result.status.value!r}"
            raise CommandError(ErrorCode.INTERNAL_ERROR, msg)

        # APPROVED on the receiver's side happens when this
        # offloader paired with the same receiver previously and
        # the receiver-side row is still APPROVED — the receiver
        # short-circuits the inbox dance. Build the row with a
        # fresh ``paired_at`` (last-touch semantic) and the
        # appropriate ``status``; the unified ``_pairings`` dict
        # holds both PENDING and APPROVED rows, and the disk
        # filter strips PENDING at serialise time.
        target_status = (
            PeerStatus.APPROVED if result.status is IntentResponse.APPROVED else PeerStatus.PENDING
        )
        pairing = StoredPairing(
            receiver_hostname=clean_host,
            receiver_port=clean_port,
            pin_sha256=result.pin_sha256,
            static_x25519_pub=result.remote_static_pub,
            label=clean_receiver_label,
            paired_at=time.time(),
            status=target_status,
        )
        key = (clean_host, clean_port)
        # Re-pair against an existing entry (same ``(host, port)``
        # but possibly a different pin / pubkey because the
        # receiver rotated its peer-link key between pair attempts)
        # replaces the dict entry; the *existing* listener task
        # captured the old pairing in its closure and would compare
        # incoming pin_sha256 against the stale value, so cancel it
        # explicitly here before deciding whether to spawn a fresh
        # listener. The cancelled task self-removes from
        # ``_pair_status_listeners`` via its ``finally`` clause.
        self._pairings[key] = pairing
        self._cancel_pair_status_listener(clean_host, clean_port)
        if target_status is PeerStatus.APPROVED:
            # Persisted trust anchor that survives restart. Schedule
            # the debounced disk write; the controller's ``stop()``
            # flushes any still-pending save through the registered
            # shutdown callback.
            self._pairings_store.async_delay_save(
                self._serialize_pairings, delay=_PAIRINGS_SAVE_DELAY_SECONDS
            )
            return _pairing_summary(pairing)
        # PENDING: in-memory only, bounded by the receiver-side
        # pairing window. The listener observes the eventual flip
        # (admin Accept) and promotes the row in
        # ``_apply_pair_status_result`` — which mutates the dict
        # entry's ``status`` and schedules a save.
        self._spawn_pair_status_listener(pairing)
        return _pairing_summary(pairing)

    @api_command("remote_build/unpair")
    async def unpair(
        self,
        *,
        hostname: str,
        port: int,
        **kwargs: Any,
    ) -> dict[str, bool]:
        """Drop the local :class:`StoredPairing` row for ``(hostname, port)``.

        Idempotent: returns ``{"removed": False}`` when no row matches
        rather than raising — the frontend's "Unpair" button should
        always succeed visually even when the row was already gone
        (race with a concurrent listener-driven flip-to-removed).

        Receiver-side state is *not* notified. The receiver's
        :class:`StoredPeer` row stays until the receiver's admin
        clicks Remove on their Pairing requests inbox; that's the
        receiver's ownership concern. The next ``intent="peer_link"``
        from this offloader will return ``REJECTED`` because the
        offloader's local row is gone, but the receiver-side row
        won't auto-clean — phase 8's re-auth wizard surfaces the
        "stale on receiver, removed locally" case as a UI affordance
        for the receiver-side admin to clean up.

        If a pair-status listener task is in flight for this row
        (admin hadn't clicked Accept/Reject yet), it gets cancelled
        promptly so the offloader's open Noise WS to the receiver
        closes cleanly rather than waiting on a now-irrelevant flip.
        """
        clean_host = _validate_hostname(hostname, context=_HostFieldContext.RECEIVER)
        clean_port = _validate_port(port, context=_HostFieldContext.RECEIVER)
        key = (clean_host, clean_port)

        # Cancel BEFORE mutating the dict: the listener task holds
        # an open Noise WS to the receiver, and we want it closed
        # promptly on user-clicks-Unpair. The cancel is sync; the
        # actual WS-close happens on the next loop iteration as the
        # cancelled task unwinds. Idempotent on absent keys (the
        # typical APPROVED-only case where no listener was ever
        # spawned). If the listener is already past the
        # cancel-checkpoint and inside ``_apply_pair_status_result``,
        # its ``self._pairings.pop(key, None)`` returns None (we
        # just popped) and the listener exits terminal without
        # promoting — no row resurrection.
        self._cancel_pair_status_listener(clean_host, clean_port)
        # Single in-RAM dict carries both PENDING and APPROVED.
        previous = self._pairings.pop(key, None)
        if previous is None:
            return {"removed": False}
        # APPROVED rows live on disk; the debounced save flushes
        # the deletion. PENDING rows aren't on disk anyway, but
        # scheduling a save on every removal keeps the code path
        # uniform — the eventual write rebuilds the pairings list
        # from RAM regardless of what the dropped row's status was.
        self._pairings_store.async_delay_save(
            self._serialize_pairings, delay=_PAIRINGS_SAVE_DELAY_SECONDS
        )
        # Fire the local bus event so other clients on the global
        # ``subscribe_events`` stream see the removal without
        # re-fetching the pairings snapshot. Mirrors the
        # receiver-side ``remove_peer`` firing the same shape.
        self._fire_offloader_pair_status_changed(clean_host, clean_port, "removed")
        return {"removed": True}

    def pairings_snapshot(self) -> list[PairingSummary]:
        """Return the in-memory pairings snapshot (PENDING + APPROVED).

        Pure synchronous read of the unified ``_pairings`` dict —
        no executor hop, no disk read, no race window.
        :meth:`start` seeded the dict from disk; every mutation
        since has flowed through the same dict, so RAM is the
        canonical source of truth.

        Used by
        :meth:`device_builder.DeviceBuilder._cmd_subscribe_events`
        to seed the frontend's initial state. Live updates flow
        from ``OFFLOADER_PAIR_STATUS_CHANGED`` bus events through
        the same ``subscribe_events`` stream — the events fire
        right after the dict mutation, so a subscriber that reads
        the snapshot then attaches its event handler will see
        every state change without missing any.

        Not a WS command: the dashboard frontend always subscribes
        on app-startup, so a separate snapshot read would just be
        a redundant round-trip.
        """
        return [_pairing_summary(p) for p in self._pairings.values()]

    def _serialize_pairings(self) -> OffloaderRemoteBuildSettings:
        """Build the on-disk shape from the in-RAM ``_pairings`` dict.

        Filters to ``status=APPROVED`` rows so a malicious LAN
        scanner can't fill the offloader's pairings file with junk
        PENDING attempts. Called by
        :meth:`Store.async_delay_save` at flush time, so the
        persisted snapshot reflects whatever's currently in RAM —
        not whatever was in RAM when the most recent mutation
        scheduled the save.
        """
        return OffloaderRemoteBuildSettings(
            pairings=[p for p in self._pairings.values() if p.status is PeerStatus.APPROVED],
        )

    # ------------------------------------------------------------------
    # Pair-status listeners (phase 4a-o part 4) — one task per PENDING
    # StoredPairing, each holding an open Noise WS to its receiver
    # with ``intent="pair_status"``. Receiver-side responder waits on
    # its own bus event for an admin click and pushes the response
    # back, so the offloader sees the flip with sub-second latency
    # without polling.
    # ------------------------------------------------------------------

    def _spawn_pair_status_listener(self, pairing: StoredPairing) -> None:
        """Spawn the pair-status listener task for *pairing* if not running.

        Called from :meth:`request_pair` after landing a fresh
        PENDING entry in ``_pairings`` (and from the same
        method on a re-pair after the prior listener was
        cancelled to avoid stale-pin closure capture). Idempotent
        on already-running listeners — returns early if a
        listener for ``(host, port)`` already exists and isn't
        done. Cold start has no PENDING entries to spawn against,
        so this isn't called from :meth:`start`.
        """
        key = (pairing.receiver_hostname, pairing.receiver_port)
        existing = self._pair_status_listeners.get(key)
        if existing is not None and not existing.done():
            return
        self._pair_status_listeners[key] = asyncio.create_task(
            self._await_pair_status_flip(pairing),
            name=f"pair-status-{key[0]}:{key[1]}",
        )

    def _cancel_pair_status_listener(self, hostname: str, port: int) -> None:
        """Cancel the listener at ``(host, port)``. No-op if none running."""
        task = self._pair_status_listeners.pop((hostname, port), None)
        if task is not None and not task.done():
            task.cancel()

    async def _await_pair_status_flip(self, pairing: StoredPairing) -> None:
        """Hold a Noise WS to the receiver until the row flips status.

        Single-shot: opens one Noise WS with ``intent="pair_status"``,
        awaits the receiver's response (which the receiver-side
        responder holds open until its own bus fires
        ``REMOTE_BUILD_PAIR_STATUS_CHANGED`` for the matching
        ``dashboard_id``), persists the result + fires
        ``OFFLOADER_PAIR_STATUS_CHANGED``, then exits. On transport
        error, sleeps :data:`_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS`
        and reconnects.
        """
        config_dir = self._db.settings.config_dir
        loop = asyncio.get_running_loop()
        peer_link_identity, dashboard_identity = await loop.run_in_executor(
            None, _load_offloader_identities, config_dir
        )
        try:
            while True:
                try:
                    result = await peer_link_await_pair_status(
                        hostname=pairing.receiver_hostname,
                        port=pairing.receiver_port,
                        identity_priv=peer_link_identity.private_bytes,
                        dashboard_id=dashboard_identity.dashboard_id,
                    )
                except PeerLinkClientError as exc:
                    _LOGGER.debug(
                        "pair-status listener for %s:%s reconnecting: %s",
                        pairing.receiver_hostname,
                        pairing.receiver_port,
                        exc,
                    )
                    await asyncio.sleep(_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS)
                    continue
                terminal = await self._apply_pair_status_result(pairing, result)
                if terminal:
                    return
                # Non-terminal result reached the apply path —
                # only happens on a misbehaving receiver returning
                # an unexpected ``intent_response`` (PENDING / OK /
                # NO_PAIRING_WINDOW from a `pair_status` query
                # shouldn't happen). Back off before reconnecting
                # so a bug in the receiver doesn't burn CPU /
                # spam logs in a tight reconnect loop.
                await asyncio.sleep(_PAIR_STATUS_RECONNECT_BACKOFF_SECONDS)
        finally:
            # Only clear the slot if it still points at this task.
            # On a re-pair, ``_cancel_pair_status_listener`` has
            # already popped this task and ``_spawn_pair_status_listener``
            # has put the replacement in the slot — blindly
            # ``pop()``-ing here would evict the replacement and
            # orphan it (no entry left for ``unpair`` to cancel,
            # the new listener parks forever).
            key = (pairing.receiver_hostname, pairing.receiver_port)
            if self._pair_status_listeners.get(key) is asyncio.current_task():
                del self._pair_status_listeners[key]

    async def _apply_pair_status_result(
        self, pairing: StoredPairing, result: PairStatusResult
    ) -> bool:
        """Apply a pair-status response. Return True when the listener should exit.

        Listener only spawns for rows the controller has in
        ``_pairings`` (typically PENDING — APPROVED rows don't
        need a listener once persisted), so each terminal branch
        either flips the row's ``status`` or drops it, schedules
        the debounced save, and fires the bus event.

        * APPROVED + matching pin → flip row to
          ``status=APPROVED``, schedule save, fire
          ``status="approved"``.
        * APPROVED + drifted pin → drop row, schedule save, fire
          ``status="removed"``. Receiver-side identity rotated
          since pair time; treat as peer-revoked rather than
          silently substituting the new pubkey under the user's
          existing trust.
        * REJECTED → drop row, schedule save, fire
          ``status="removed"``. Receiver returned this when admin
          clicked Reject, the window closed (clearing the
          receiver-side pending dict), the offloader's identity
          rotated, or the row never existed on the receiver. From
          the offloader's POV all four cases collapse to "drop
          the local row, user can re-pair if they want."
        * PENDING / OK / NO_PAIRING_WINDOW shouldn't appear here
          — the long-poll only returns APPROVED or REJECTED. Log
          + reconnect on the off-chance a future receiver bug
          emits something unexpected.

        Race-safe against ``unpair``: every branch pops or mutates
        via a single ``pop``-keyed comparison; if the user ran
        ``unpair`` between our ``await await_pair_status(...)``
        and this branch the row is already gone, so the controller
        skips both promotion and event-firing. ``unpair`` itself
        fires ``status="removed"`` so other subscribed clients
        still see the removal.
        """
        host = pairing.receiver_hostname
        port = pairing.receiver_port
        key = (host, port)
        if result.status is IntentResponse.APPROVED:
            if result.pin_sha256 != pairing.pin_sha256:
                _LOGGER.warning(
                    "pair-status pin drift for %s:%s; dropping row (stored=%s observed=%s)",
                    host,
                    port,
                    pairing.pin_sha256,
                    result.pin_sha256,
                )
                if self._pairings.pop(key, None) is not None:
                    # PENDING dropped: not on disk anyway. If a
                    # previously-APPROVED row drifted-pin lands here
                    # in some future flow, the schedule_save still
                    # evicts it from disk — keep the path uniform.
                    self._pairings_store.async_delay_save(
                        self._serialize_pairings, delay=_PAIRINGS_SAVE_DELAY_SECONDS
                    )
                    self._fire_offloader_pair_status_changed(host, port, "removed")
                return True
            # Promote PENDING → APPROVED in place. ``unpair``
            # between our ``await await_pair_status(...)`` and this
            # branch would have already popped + cancelled this
            # listener; if the row's gone, do nothing — writing it
            # back would resurrect state the user just deleted.
            # ``unpair`` itself fires ``OFFLOADER_PAIR_STATUS_CHANGED``
            # so any other subscribed client (other tabs) sees the
            # removal; we exit terminal silently here, no second
            # event needed.
            existing = self._pairings.get(key)
            if existing is None:
                return True
            existing.status = PeerStatus.APPROVED
            self._pairings_store.async_delay_save(
                self._serialize_pairings, delay=_PAIRINGS_SAVE_DELAY_SECONDS
            )
            self._fire_offloader_pair_status_changed(host, port, "approved")
            return True
        if result.status is IntentResponse.REJECTED:
            if self._pairings.pop(key, None) is not None:
                self._pairings_store.async_delay_save(
                    self._serialize_pairings, delay=_PAIRINGS_SAVE_DELAY_SECONDS
                )
                self._fire_offloader_pair_status_changed(host, port, "removed")
            return True
        _LOGGER.warning(
            "pair-status returned unexpected status %r for %s:%s",
            result.status,
            host,
            port,
        )
        return False

    def _fire_offloader_pair_status_changed(
        self,
        receiver_hostname: str,
        receiver_port: int,
        status: Literal["approved", "removed"],
    ) -> None:
        """Fire ``OFFLOADER_PAIR_STATUS_CHANGED`` for a pairing flip.

        Mirrors :meth:`_fire_pair_status_changed` (receiver-side)
        for shape; both methods are the named-intent boundary
        between controller logic and the bus payload format.
        """
        payload: OffloaderPairStatusChangedData = {
            "receiver_hostname": receiver_hostname,
            "receiver_port": receiver_port,
            "status": status,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_STATUS_CHANGED, payload)

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
                RemoteBuildIdentityRotatedData(
                    dashboard_id=identity.dashboard_id,
                    pin_sha256=identity.pin_sha256,
                ),
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

    @api_command("remote_build/approve_peer")
    async def approve_peer(self, *, dashboard_id: str, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Promote a PENDING peer to APPROVED.

        Pops the in-memory PENDING entry, inserts it into the
        RAM-canonical ``_approved_peers`` dict, schedules a
        debounced write to the receiver-peers store, and fires
        :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED` with
        ``{dashboard_id, status: "approved"}``. The offloader's
        pair-status listener observes the flip via the bus event +
        re-snapshot path. ``NOT_FOUND`` if no PENDING entry
        matches; ``INVALID_ARGS`` if the dashboard_id already
        corresponds to an APPROVED row (duplicate Accept click,
        almost always a UI race; refuse rather than silently
        re-fire the event).
        """
        clean_id = _validate_dashboard_id(dashboard_id)

        pending = self._pending_peers.pop(clean_id, None)
        if pending is None:
            # Differentiate "already approved" from "never existed"
            # so the frontend can decide whether to refresh or
            # surface an error. Both reads short-circuit through
            # RAM — no disk I/O.
            if clean_id in self._approved_peers:
                msg = f"peer is already approved: {clean_id}"
                raise CommandError(ErrorCode.INVALID_ARGS, msg)
            msg = f"no pending peer with dashboard_id: {clean_id}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)

        self._approved_peers[clean_id] = pending
        self._peers_store.async_delay_save(
            self._serialize_peers, delay=_PAIRINGS_SAVE_DELAY_SECONDS
        )
        self._fire_pair_status_changed(clean_id, "approved")
        return await self._current_settings_view()

    @api_command("remote_build/remove_peer")
    async def remove_peer(self, *, dashboard_id: str, **kwargs: Any) -> RemoteBuildSettingsView:
        """
        Delete a peer row (works on both PENDING and APPROVED).

        Two semantically distinct outcomes share the same WS command:

        * Removing a PENDING entry from the in-memory dict is
          *rejection* — the row never represented established
          trust, so this is inbox cleanup. Fires the
          ``status="removed"`` event so any offloader currently
          long-polling pair_status sees the cancellation and
          drops its local state.
        * Removing an APPROVED row from ``_approved_peers``
          (RAM-canonical, debounced to disk) is *revocation* —
          fires the same
          :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED`
          ``status="removed"`` event so the offloader can
          surface a ``peer_revoked`` UI alert (phase 4b-3).

        ``NOT_FOUND`` if neither dict has a row.
        """
        clean_id = _validate_dashboard_id(dashboard_id)

        # PENDING: in-memory, no disk write needed (PENDING never
        # reaches the peers store).
        if self._pending_peers.pop(clean_id, None) is not None:
            self._fire_pair_status_changed(clean_id, "removed")
            return await self._current_settings_view()

        if self._approved_peers.pop(clean_id, None) is None:
            msg = f"no peer with dashboard_id: {clean_id}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        self._peers_store.async_delay_save(
            self._serialize_peers, delay=_PAIRINGS_SAVE_DELAY_SECONDS
        )
        self._fire_pair_status_changed(clean_id, "removed")
        return await self._current_settings_view()

    def _serialize_peers(self) -> ReceiverPeers:
        """Build the on-disk shape from the in-RAM ``_approved_peers`` dict.

        Called by :meth:`Store.async_delay_save` at flush time, so
        the persisted snapshot reflects whatever's currently in
        RAM — not whatever was in RAM when the most recent
        mutation scheduled the save. Mirrors the offloader-side
        :meth:`_serialize_pairings` shape.
        """
        return ReceiverPeers(peers=list(self._approved_peers.values()))

    async def _current_settings_view(self) -> RemoteBuildSettingsView:
        """Load settings from disk and project to the wire view.

        Helper for response paths that need a fresh
        :class:`RemoteBuildSettingsView` after a mutation that
        only touched RAM (peers). The view's ``peers`` field
        reads from RAM via :meth:`_to_view`; the ``enabled`` /
        ``manual_hosts`` fields still come from the metadata
        sidecar.
        """
        loop = asyncio.get_running_loop()
        settings = await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )
        return self._to_view(settings)

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

        Returns:
        * :attr:`IntentResponse.APPROVED` — a row already exists
          for this ``dashboard_id`` with status APPROVED **and**
          its stored pin matches the handshake's. Returns
          ``APPROVED`` without changing the row or firing the
          event; demoting an already-trusted peer back to PENDING
          on every stray pair_request would force the receiving
          dashboard's user to re-approve on every offloader
          hiccup, which is hostile UX. **Bypasses the pairing
          window** — the offloader is re-establishing existing
          trust, not asking for new authorization, so admin
          doesn't need to be on the Pairing requests screen.
        * :attr:`IntentResponse.PENDING` — created a new
          ``StoredPeer`` (or refreshed an existing PENDING row's
          pin / label / paired_at). Only reachable while the
          pairing window is open — both branches that would
          create / refresh a PENDING row check
          :meth:`is_pairing_window_open` first and return
          ``NO_PAIRING_WINDOW`` when closed (admin has to be on
          the Pairing requests screen for new authorization). On
          the PENDING path, fires
          :attr:`EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED` so
          the receiver UI surfaces the request in the inbox.
        * :attr:`IntentResponse.REJECTED` — a row exists for this
          ``dashboard_id`` with status APPROVED but the
          handshake's pin doesn't match the stored pin. Either
          the offloader rotated their identity under us, or
          someone is presenting a fresh keypair and claiming
          Alice's ``dashboard_id``. Refuse regardless of window
          state; the receiver-side user has to remove the peer
          and re-pair if the rotation is legitimate.
        * :attr:`IntentResponse.NO_PAIRING_WINDOW` — admin isn't
          on the Pairing requests screen, and this request would
          create or refresh a PENDING row. Returned for the
          unknown-dashboard_id branch and the existing-PENDING
          branch only.
        """
        # Already-APPROVED branch — RAM read, no disk hop. The
        # dict is the source of truth at runtime; ``start`` seeded
        # it from disk and every approve / remove flows through
        # the same dict.
        approved_peer = self._approved_peers.get(dashboard_id)
        if approved_peer is not None:
            if approved_peer.pin_sha256 != pin_sha256:
                # Pin mismatch on an APPROVED row is a
                # rotation-or-impersonation signal; refuse rather
                # than silently re-approve under the new identity.
                # Independent of window state.
                return IntentResponse.REJECTED
            # Already-approved + pin still matches: re-pair against
            # existing trust. No admin action needed, so window
            # state is irrelevant.
            return IntentResponse.APPROVED

        # New or pending — gated on the pairing window so admins
        # can refuse to even accumulate inbox noise (in memory or
        # on disk) from arbitrary LAN scanners. The dict-only
        # storage means a malicious LAN client can't fill the
        # receiver's persistent state with junk pair-requests
        # even within an open window — the dict is bounded by
        # window lifetime + cleared on auto-close.
        if not self.is_pairing_window_open():
            return IntentResponse.NO_PAIRING_WINDOW

        # Add or refresh the in-memory PENDING entry. The dict is
        # keyed on dashboard_id so a re-pair while still pending
        # (offloader retried before admin clicked) overwrites the
        # earlier dict entry rather than creating a duplicate.
        # Single ``paired_at`` shared between the StoredPeer and
        # the event payload so a future refactor can't accidentally
        # split them — frontend subscribers building a complete row
        # from the event need the same timestamp the inbox
        # snapshot would have shown.
        paired_at = time.time()
        self._pending_peers[dashboard_id] = StoredPeer(
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            static_x25519_pub=static_x25519_pub,
            label=label,
            paired_at=paired_at,
            peer_ip=peer_ip,
        )
        payload: RemoteBuildPairRequestReceivedData = {
            "dashboard_id": dashboard_id,
            "pin_sha256": pin_sha256,
            "label": label,
            "peer_ip": peer_ip,
            "paired_at": paired_at,
        }
        self._db.bus.fire(EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED, payload)
        return IntentResponse.PENDING

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
        * :attr:`IntentResponse.PENDING` — peer's row exists in
          the receiver's in-memory PENDING dict (admin hasn't
          clicked Accept yet). The offloader's frontend reflects
          this via the offloader-side ``OFFLOADER_PAIR_STATUS_CHANGED``
          event stream rather than retrying ``peer_link``;
          ``peer_link`` is for established sessions, not for
          waiting on admin approval — that's the
          ``intent="pair_status"`` long-poll's job.
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
        Resolve an ``intent="pair_status"`` query, long-polling on PENDING.

        Returns:
        * :attr:`IntentResponse.APPROVED` — peer is APPROVED.
          Snapshot path; returns immediately.
        * :attr:`IntentResponse.REJECTED` — no row matches OR pin
          mismatch. Reached three ways: (a) the offloader has
          never paired, (b) admin clicked Reject (deletes the
          dict entry), (c) the offloader's peer-link identity
          rotated under us, (d) the receiver's pairing window
          closed mid-wait — window-close clears the pending
          dict and fires ``REMOTE_BUILD_PAIR_STATUS_CHANGED``
          ``status="removed"`` for each cleared entry, the
          flip-event wakes the long-poll, and the re-snapshot
          finds no matching row. The offloader treats REJECTED
          as a peer-revoked signal regardless of which path
          produced it: drop the local row.

        Long-poll semantics: with the snapshot at PENDING, await
        :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED` for
        the matching ``dashboard_id``. No timeout — the WS hangs
        until either the offloader cancels its end, an admin
        click flips the row (event fires APPROVED), or
        window-close clears the dict (event fires "removed",
        re-snapshot returns REJECTED). Receiver-side cost is one
        parked task per pending peer, bounded by the pending-row
        count.

        Window-gating is *implicit* now that PENDING peers live
        in an in-memory dict cleared on window-close: when the
        window is closed there are no PENDING entries to
        snapshot, so the snapshot returns REJECTED immediately
        and the long-poll never starts. The ``is_pairing_window_open()``
        check at the entry path is unnecessary and absent.
        ``NO_PAIRING_WINDOW`` is *not* a return value here —
        closed-window manifests as REJECTED via the empty-dict
        path; an earlier draft of this method gated explicitly
        on the window and returned NO_PAIRING_WINDOW, but the
        in-memory-dict refactor collapsed both cases to REJECTED
        (cleaner offloader-side branch table — listener treats
        REJECTED as terminal, doesn't need a separate "window
        closed, retry later" branch).

        Listener registration order is load-bearing: bus
        ``listening`` attaches BEFORE the snapshot read so an
        ``approve_peer`` firing between snapshot and wait can't
        slip past us. Re-snapshot after the flip wakes us keeps
        the response source-of-truth in one place
        (:meth:`_lookup_peer_response`) and naturally handles
        pin-drift (e.g. offloader rotated peer-link key between
        pair and poll → REJECTED on the re-snapshot).

        Differs from :meth:`lookup_peer_for_session` in just
        one way: APPROVED returns ``APPROVED`` vs ``OK`` because
        pair_status is informational while peer_link is
        connection-establishing. Both paths consult the same
        dict + list pair via :meth:`_lookup_peer_response`.
        """
        flip_event = asyncio.Event()

        def _on_pair_status(event: Event[RemoteBuildPairStatusChangedData]) -> None:
            if event.data["dashboard_id"] == dashboard_id:
                flip_event.set()

        with self._db.bus.listening([EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED], _on_pair_status):
            snapshot = await self._lookup_peer_response(
                dashboard_id=dashboard_id,
                pin_sha256=pin_sha256,
                approved_response=IntentResponse.APPROVED,
            )
            if snapshot is not IntentResponse.PENDING:
                return snapshot
            await flip_event.wait()
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

        Walks the in-memory PENDING dict first, then the persisted
        APPROVED list. Both intents need the same pin-match check
        on either store; only the APPROVED return value differs
        (caller passes :attr:`IntentResponse.OK` for peer_link,
        :attr:`IntentResponse.APPROVED` for pair_status).

        Returns ``REJECTED`` when no row matches OR pin doesn't
        match — the offloader treats either case the same (drop
        local row + surface re-pair UI).
        """
        # PENDING dict first — most pair-flow traffic is pending
        # peers polling pair_status. Both lookups are RAM reads
        # (the APPROVED list moved off disk into ``_approved_peers``
        # at startup).
        pending = self._pending_peers.get(dashboard_id)
        if pending is not None:
            if pending.pin_sha256 != pin_sha256:
                return IntentResponse.REJECTED
            return IntentResponse.PENDING
        peer = self._approved_peers.get(dashboard_id)
        if peer is None or peer.pin_sha256 != pin_sha256:
            return IntentResponse.REJECTED
        return approved_response

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
        # Closed-transition: clear the in-memory PENDING dict +
        # notify any in-flight pair_status long-polls that their
        # row is gone. Mirror of the auto-close path.
        if was_open and not is_open:
            self._clear_pending_peers_on_window_close()
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
        every client has aged out. Clear the client refcount + the
        in-memory PENDING peers dict, fire the close event +
        cancellation events, done.
        """
        self._pairing_window_handle = None
        self._pairing_window_clients.clear()
        self._fire_pairing_window_changed()
        self._clear_pending_peers_on_window_close()

    def _clear_pending_peers_on_window_close(self) -> None:
        """Drop every PENDING peer + fire removal events.

        Called from the window-close transition paths (auto-close
        timer fire, explicit ``set_pairing_window(open=False)``,
        controller stop). Fires
        :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED`
        ``status="removed"`` for each cleared row so any offloader
        currently long-polling :meth:`lookup_peer_for_status`
        wakes, re-snapshots (now misses), and returns ``REJECTED``
        to its listener — which drops the offloader's local
        StoredPairing and surfaces "admin walked away" to the
        user.

        Idempotent — calling on an empty dict is a no-op.
        """
        if not self._pending_peers:
            return
        cleared = list(self._pending_peers)
        self._pending_peers.clear()
        for dashboard_id in cleared:
            self._fire_pair_status_changed(dashboard_id, "removed")


def _identity_view(identity: DashboardIdentity, *, listener_bound: bool) -> IdentityView:
    """Project a :class:`DashboardIdentity` into the wire shape."""
    return IdentityView(
        dashboard_id=identity.dashboard_id,
        pin_sha256=identity.pin_sha256,
        server_version=server_version,
        esphome_version=esphome_version,
        listener_bound=listener_bound,
    )
