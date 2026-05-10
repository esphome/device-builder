"""Remote-build feature models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Literal, TypedDict

import voluptuous as vol
from mashumaro.mixins.orjson import DataClassORJSONMixin

from ..helpers.voluptuous_validators import lowercase_hex, not_bool


class RemoteBuildPeerSource(StrEnum):
    """
    How a peer dashboard ended up in :meth:`list_hosts`.

    ``mdns``: discovered via the ``_esphomebuilder._tcp.local.``
    browse. ``manual``: added by the user via
    ``remote_build/add_manual_host`` for cross-subnet or
    non-multicast LANs where mDNS doesn't reach but L3 unicast
    does.
    """

    MDNS = "mdns"
    MANUAL = "manual"


@dataclass
class ManualHost(DataClassORJSONMixin):
    """
    A user-supplied peer entry stored in the metadata sidecar.

    Persisted under ``_remote_build.manual_hosts``; merged into
    :meth:`list_hosts` output as a :class:`RemoteBuildPeer` row
    with ``source=MANUAL`` and empty version fields. Phase 2b does
    no version / fingerprint resolution; phase 4 attempts the
    connection and fills the version fields in.
    """

    hostname: str
    port: int


class PeerStatus(StrEnum):
    """
    Lifecycle state of a :class:`StoredPeer` row.

    ``PENDING``: an offloader's pair-request landed and the
    receiver's admin hasn't accepted yet. The peer-link auth
    gate lets a connection from this peer's pubkey complete the
    Noise handshake but only honours an ``intent="pair_status"``
    query; every other intent is rejected at the post-handshake
    dispatch.

    ``APPROVED``: admin clicked Accept. Full access — the auth
    gate looks up the offloader's static X25519 pubkey hash
    (extracted from the Noise XX handshake transcript) against
    this row on every connection.

    No explicit ``REJECTED`` terminal state — a rejected request
    deletes the row. If the same offloader retries, it lands as
    a fresh pending row and the admin chooses again. Avoids the
    bookkeeping a rejected-list would need; phase 8's re-auth
    wizard can revisit if blocklisting becomes useful.
    """

    PENDING = "pending"
    APPROVED = "approved"


class PeerLinkIntent(StrEnum):
    """
    Wire ``intent`` discriminator on the peer-link Noise WS msg1 payload.

    Sent in cleartext on msg1 (Noise XX hasn't established a key
    yet for that frame's payload) so the receiver can route the
    session before the handshake completes. The sensitive
    metadata (``dashboard_id``, ``label``) waits until msg3,
    which is encrypted under the now-finalized cipher.

    * ``PREVIEW`` — capture the receiver's static pubkey for
      OOB pin verification. Doesn't change any receiver state.
    * ``PAIR_REQUEST`` — gated by the pairing window from #106
      design choice (c). Creates / refreshes a PENDING
      ``StoredPeer`` row and fires
      ``REMOTE_BUILD_PAIR_REQUEST_RECEIVED``.
    * ``PEER_LINK`` — establishes a peer-link session for an
      already-APPROVED peer. Phase 5+ keeps the WS open for
      application messages (bundle upload, build, firmware
      download); part 4 just answers the handshake.
    * ``PAIR_STATUS`` — informational poll for a previously-
      submitted pair_request's current state.
    """

    PREVIEW = "preview"
    PAIR_REQUEST = "pair_request"
    PEER_LINK = "peer_link"
    PAIR_STATUS = "pair_status"


class IntentResponse(StrEnum):
    """
    Wire ``intent_response`` value the receiver returns over the peer-link.

    Sent in the post-handshake transport frame after the Noise XX
    handshake completes. ``StrEnum`` so members serialise to their
    wire string verbatim through ``json.dumps`` and so equality
    comparisons against the raw string still work for callers that
    haven't migrated yet.

    Per-intent semantics (cross-referenced with #106 design choice
    (h)):

    * ``OK`` — success on ``intent="preview"`` (handshake captured
      pubkey, nothing else needed) or on ``intent="peer_link"``
      from an APPROVED peer (caller can keep the WS open for
      application messages in phase 5+).
    * ``APPROVED`` — ``intent="pair_status"`` poll observing an
      APPROVED row, or ``intent="pair_request"`` from a peer
      that's already APPROVED (we don't demote them; the offloader
      is expected to switch to ``intent="peer_link"``).
    * ``PENDING`` — ``intent="pair_request"`` created or refreshed
      a PENDING row, or ``intent="pair_status"`` /
      ``intent="peer_link"`` polled a row that's still PENDING.
    * ``REJECTED`` — unknown ``dashboard_id``, pin mismatch
      (handshake's pubkey doesn't match the stored row), or
      unknown ``intent``. The offloader's UI surfaces a
      "send a fresh pair_request" CTA.
    * ``NO_PAIRING_WINDOW`` — ``intent="pair_request"`` arrived
      while the receiver-side pairing window is closed; no row
      created. The offloader's UI prompts the user to ask the
      receiving dashboard's user to open the Pairing requests
      screen.
    """

    OK = "ok"
    APPROVED = "approved"
    PENDING = "pending"
    REJECTED = "rejected"
    NO_PAIRING_WINDOW = "no_pairing_window"


# ---------------------------------------------------------------------------
# Event payload shapes (TypedDict so the bus.fire data dict is type-checked at
# the call site without changing the wire shape; mirrors HA's
# ``EventStateChangedData`` / ``EventStateReportedData`` pattern).
# ---------------------------------------------------------------------------


class RemoteBuildIdentityRotatedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_IDENTITY_ROTATED``.

    Fired after ``rotate_certificate`` succeeds and the new
    ``pin_sha256`` is reloaded into the listener. Subscribers
    (the offloader-side peer-link in phase 4+, the receiver
    Settings UI in 3c2) refresh their cached pin without polling
    ``get_identity``. The event reflects only that the cert + key
    on disk changed; the listener rebuild may still fail-soft, in
    which case the rotater's ``IdentityView`` response carries
    ``listener_bound=False``.
    """

    dashboard_id: str
    pin_sha256: str


class RemoteBuildPairRequestReceivedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED``.

    Fired by the peer-link Noise WS handler when a fresh
    ``intent="pair_request"`` arrives during an open pairing
    window. The receiver Settings UI surfaces the row in the
    Pairing requests inbox; ``peer_ip`` lets the operator
    sanity-check the source against expectations before
    OOB-confirming the pin.

    ``paired_at`` carries the receiver-clock unix timestamp at
    row creation, matching the ``StoredPeer.paired_at`` field
    on the in-memory PENDING entry. Lets a subscriber rendering
    the inbox from the event stream sort the most-recent attempt
    first without a follow-up snapshot read.
    """

    dashboard_id: str
    pin_sha256: str
    label: str
    peer_ip: str
    paired_at: float


class RemoteBuildPairStatusChangedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED``.

    Fires from three paths:

    * ``approve_peer`` promoting a PENDING dict entry to
      APPROVED (``status="approved"``).
    * ``remove_peer`` dropping either a PENDING dict entry or
      an APPROVED list row (``status="removed"``).
    * Pairing-window-close clearing the in-memory PENDING dict
      (``status="removed"`` per cleared entry).

    The ``status="removed"`` event is what wakes any in-flight
    ``intent="pair_status"`` long-poll on a paired offloader so
    its listener task drops the offloader's local state.
    """

    dashboard_id: str
    status: Literal["approved", "removed"]


class OffloaderPairStatusChangedData(TypedDict):
    """
    Payload for ``EventType.OFFLOADER_PAIR_STATUS_CHANGED``.

    Offloader-side counterpart to
    :class:`RemoteBuildPairStatusChangedData`. Fired on the
    offloader's local bus from two paths:

    * The per-row pair-status listener task
      (``RemoteBuildController._await_pair_status_flip`` →
      ``_apply_pair_status_result`` → ``_fire_offloader_pair_status_changed``)
      when a previously-PENDING :class:`StoredPairing` flips to
      ``APPROVED`` (admin clicked Accept) or is dropped because
      the receiver returned ``REJECTED`` (admin clicked Reject;
      window closed clearing the receiver-side dict; row never
      existed; pin rotated).
    * ``RemoteBuildController.unpair`` when the user removes a
      row, so other clients on the global ``subscribe_events``
      stream see the removal without re-fetching the pairings
      snapshot.

    Delivered to clients via the existing global
    ``subscribe_events`` stream — no separate subscription
    channel.

    The two events have the same wire shape but live on
    different buses (receiver vs offloader) and key on
    different identifiers — the offloader's
    :class:`StoredPairing` never stores the receiver's
    ``dashboard_id``, so the receiver coordinates are the
    ``(hostname, port)`` the user originally dialled.
    """

    receiver_hostname: str
    receiver_port: int
    status: Literal["approved", "removed"]


class RemoteBuildPairingWindowChangedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIRING_WINDOW_CHANGED``.

    Fires whenever the in-process pairing window opens, extends,
    or closes. ``expires_in_seconds`` is ``None`` when ``open`` is
    ``False``; otherwise it's the remaining lifetime against the
    latest extend, which the receiver-side frontend renders as a
    live countdown.
    """

    open: bool
    expires_in_seconds: float | None


@dataclass
class StoredPeer(DataClassORJSONMixin):
    """
    Receiver-side record of a paired (or pending) offloader.

    Persisted under ``_remote_build.peers``. Created by the
    pair-request flow over the peer-link WS: an offloader runs
    a Noise XX handshake with ``intent="pair_request"`` and a
    payload carrying its ``label`` + ``dashboard_id``. The
    receiver reads the offloader's static X25519 pubkey from
    the Noise handshake itself (no cert involved) and stores it.

    ``static_x25519_pub`` is the canonical identifier the
    Noise handshake binds to. ``pin_sha256`` is its lowercase-
    hex SHA-256, used for log lines / event payloads / wire
    fields where we already have a hex-pin convention.

    ``dashboard_id`` is the offloader's stable identity from
    phase 3a; sent in the pair_request payload so the admin UI
    has a friendly identifier (the X25519 pubkey alone doesn't
    carry one). Primary key for the receiver WS surface
    (``approve_peer({dashboard_id})`` etc.) so a future X25519
    keypair rotation on the offloader's side doesn't change the
    user-facing handle.

    ``label`` is a human-readable name the offloader's user
    sets during pair (e.g. ``green``, ``laptop``).

    ``peer_ip`` is the source IP we observed the offloader's
    pair_request handshake from. Persisted (rather than carried
    only on the live event) so the receiver Settings inbox can
    surface it for clone-risk sanity-check on rows that landed
    before the admin opened the page. Empty string when unknown
    — legacy rows from receivers that pre-date this field load
    cleanly with an empty default and the frontend hides the IP
    line when blank.
    """

    dashboard_id: str
    pin_sha256: str
    static_x25519_pub: bytes
    label: str
    paired_at: float
    peer_ip: str = ""

    def refresh_from_pair_request(
        self,
        *,
        pin_sha256: str,
        static_x25519_pub: bytes,
        label: str,
        paired_at: float,
        peer_ip: str,
    ) -> None:
        """
        Update the fields a fresh ``intent="pair_request"`` supplies.

        Owns the contract for "what changes on re-pair": the X25519
        pubkey + its hash (offloader rotated their identity), the
        label (renamed dashboard), the ``paired_at`` timestamp (so
        the receiver-side inbox sorts the most-recent attempt
        first), and the source ``peer_ip`` (offloader could have
        moved interfaces / DHCP renewed). ``dashboard_id`` is the
        row's primary key and is intentionally left out of the
        refresh set; ``status`` is also left out because
        pair_request never changes status by itself (the
        receiver-side user's Accept / Reject does, via
        ``approve_peer`` / ``remove_peer``).

        Caller is responsible for the no-demote-when-APPROVED
        check before invoking this; calling
        ``refresh_from_pair_request`` on an APPROVED row would
        silently overwrite the originally-pinned pubkey, which is
        the wrong outcome (see ``record_pair_request`` for the
        gating logic).
        """
        self.pin_sha256 = pin_sha256
        self.static_x25519_pub = static_x25519_pub
        self.label = label
        self.paired_at = paired_at
        self.peer_ip = peer_ip


@dataclass
class PeerSummary(DataClassORJSONMixin):
    """
    Public-facing wire view of :class:`StoredPeer`.

    Drops ``static_x25519_pub`` — the raw 32-byte pubkey is
    on-disk only; ``pin_sha256`` (lowercase-hex SHA-256 of the
    pubkey) is the wire-friendly form that UIs render for
    OOB-verification. ``peer_ip`` is the source IP observed at
    pair_request time; the receiver Settings inbox renders it
    next to the pin so the operator has a second sanity-check
    against a clone scenario (an attacker on a different IP
    submitting a pair_request with a spoofed label or against a
    drifted dashboard_id). Empty string for legacy rows from
    receivers that pre-date the persisted ``peer_ip`` field.
    """

    dashboard_id: str
    pin_sha256: str
    label: str
    paired_at: float
    status: PeerStatus
    peer_ip: str = ""


# Bounds enforced both at the WS-command boundary (the future
# 4a-o ``request_pair`` validators) and at the storage seam
# (``StoredPairing.__post_init__``). Defense-in-depth for the
# storage layer keeps a hand-edited sidecar from smuggling huge
# values past the WS validators — the on-disk shape is the same
# trust surface as anything else under ``<config_dir>``, and a
# malformed row is the kind of thing the loader's
# ``DataClassORJSONMixin.from_dict`` happily round-trips
# regardless of size.
#
# Schema is voluptuous because (a) it's already a transitive
# dep through ESPHome's ``config_validation``, (b) declarative
# field bounds beat hand-rolled if-chains for readability, and
# (c) the upstream ESPHome codebase uses the same primitive so
# contributors moving between repos see a familiar shape.
#
# **Maintenance note.** The schema below is a second source of
# truth alongside the dataclass field annotations: each
# ``vol.Required(...)`` mirrors a field declared on
# :class:`StoredPairing`, and dropping or renaming a field in
# one place without the other will silently desync (the schema
# would either fail-pass — ``vol.Required`` against a missing
# key raises — or accept-everything if the annotation widens).
# Keep the two in lockstep when changing the row shape: add /
# rename / remove the dataclass field AND the corresponding
# schema entry in the same change.
#
# **Asymmetry with :class:`StoredPeer`.** The receiver-side
# row doesn't run a comparable schema in
# ``__post_init__``: ``record_pair_request`` is its only
# constructor in production, and that path runs after a
# successful Noise XX handshake (the noiseprotocol library
# guarantees ``static_x25519_pub`` is exactly 32 bytes; the
# dispatcher validates ``dashboard_id`` against
# ``DASHBOARD_ID_PATTERN`` and caps ``label`` via
# ``_normalize_label`` *before* reaching the controller). A
# follow-up applying the same storage-seam validator to
# ``StoredPeer`` for symmetry is on the 4a-o follow-up list;
# for now this PR is documenting the inconsistency rather
# than masking it.
_PAIRING_VALIDATOR = vol.Schema(
    {
        # RFC 1035 §2.3.4 caps a fully-qualified domain name at 253
        # characters; round up to 255 to leave room for trailing-dot
        # variations. The ``\S`` requirement rejects whitespace-only
        # values that would otherwise pass ``Length(min=1)`` —
        # storing a hostname that can't resolve is worse than
        # rejecting at write time. Normalisation (strip + lowercase)
        # is the WS-command validator's job; the storage seam just
        # rejects malformed rows.
        vol.Required("receiver_hostname"): vol.All(
            str, vol.Length(min=1, max=255), vol.Match(r"\S")
        ),
        # :func:`not_bool` first because voluptuous's ``int`` check
        # accepts ``bool`` (Python's ``isinstance(True, int)`` is true) —
        # without the explicit reject, ``receiver_port=True`` would pass
        # as port 1.
        vol.Required("receiver_port"): vol.All(not_bool, int, vol.Range(min=1, max=65535)),
        # Lowercase-hex SHA-256: 64 chars from ``[0-9a-f]``. Factory
        # in ``helpers/voluptuous_validators.py`` so the same shape
        # can be reused by the future ``StoredPeer`` validator (issue
        # #106 follow-up) and the 4a-o parts 2-3 WS-command
        # validators without drifting.
        vol.Required("pin_sha256"): lowercase_hex(64),
        # ``static_x25519_pub`` is the raw X25519 pubkey — exactly 32
        # bytes per RFC 7748 §5.
        vol.Required("static_x25519_pub"): vol.All(bytes, vol.Length(min=32, max=32)),
        vol.Required("label"): vol.All(str, vol.Length(max=128)),
        # ``vol.All(not_bool, ...)`` rather than ``vol.Any(int, float,
        # not_bool)`` because ``Any`` short-circuits on the first
        # accepting branch — ``int`` would accept ``True``
        # (``isinstance(True, int)`` is true) before ever reaching the
        # bool reject. Run ``not_bool`` first, then assert int-or-float.
        vol.Required("paired_at"): vol.All(not_bool, vol.Any(int, float)),
        # ``status`` is in-memory only (the controller's serialiser
        # filters PENDING rows out before writing to disk so the
        # on-disk shape stays APPROVED-only), but ``__post_init__``
        # runs the validator over ``asdict(self)`` which includes
        # the field, so the schema must accept it. ``vol.In(PeerStatus)``
        # matches both the enum instance (live constructor paths)
        # and the bare string forms (``"pending"`` / ``"approved"``)
        # that ``DataClassORJSONMixin.from_dict`` produces when
        # round-tripping through JSON.
        vol.Required("status"): vol.In(PeerStatus),
    }
)


@dataclass
class StoredPairing(DataClassORJSONMixin):
    """
    Offloader-side record of a paired (or pending) receiver.

    Persisted in the per-file
    :class:`~helpers.storage.Store` at
    ``<config_dir>/.offloader_pairings.json`` (RAM-first model:
    the controller's ``_pairings`` dict is the runtime source of
    truth, and the ``Store`` debounce-saves APPROVED rows to
    disk). Created by the ``request_pair`` flow over the
    peer-link WS: the offloader runs a Noise XX handshake with
    ``intent="pair_request"``, captures the receiver's static
    X25519 pubkey from the handshake transcript, and stores it
    here together with the receiver's ``(hostname, port)``
    coordinates.

    This is the offloader-side counterpart to
    :class:`StoredPeer` (receiver-side). The two shapes are
    deliberately *not* the same row passed both ways: the
    receiver's ``StoredPeer`` keys on the offloader's
    ``dashboard_id`` (the offloader's stable identity), while
    the offloader's ``StoredPairing`` keys on
    ``(receiver_hostname, receiver_port)`` because that's what
    the user enters in the Pair dialog and what reconnection
    needs.

    ``static_x25519_pub`` is the canonical identifier the Noise
    handshake binds to. ``pin_sha256`` is its lowercase-hex
    SHA-256 (the OOB-verified pin the user confirmed on the
    receiver's Build server card). Both are stored so a future
    re-pair can detect a receiver-side identity rotation
    (handshake's pubkey-hash drifts from the stored
    ``pin_sha256``) without re-deriving from the raw bytes on
    every connect.

    ``label`` is the human-readable name the offloader's user
    typed when pairing (e.g. ``desktop``, ``build server``).
    Surfaced in the offloader's settings list; not sent to the
    receiver — the receiver gets its own ``label`` from the
    offloader's pair_request payload (the offloader-supplied
    name FOR the offloader, not for the receiver).

    ``status`` is the row's lifecycle position. The controller
    holds one in-RAM dict containing both PENDING and APPROVED
    rows; the disk filter in ``_serialize_pairings`` strips
    PENDING rows so the on-disk shape stays APPROVED-only.
    ``status`` defaults to ``APPROVED`` for two reasons:

    * **Disk shape invariant** — only APPROVED rows ever reach
      disk, so reading a row back from the per-file ``Store``
      always produces an APPROVED row. The default matches the
      invariant.
    * **Test ergonomics** — fixtures and ad-hoc constructions
      that don't care about lifecycle (most of the
      validator-shape tests, the reflection-driven event-payload
      contracts) don't have to thread a status arg through.

    PENDING is the explicit case — ``request_pair`` sets it on
    the row before adding it to the dict; ``_apply_pair_status_result``
    flips it to APPROVED in place when the receiver reports the
    flip.

    ``__post_init__`` enforces upper bounds on the user-supplied
    string fields (hostname, label) + shape on the cryptographic
    fields so a malformed sidecar row (hand-edit, partial-write
    recovery, schema-skew across ESPHome upgrades) is rejected
    by the loader's ``from_dict`` rather than landing as a
    multi-megabyte string in memory + on the wire.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    static_x25519_pub: bytes
    label: str
    paired_at: float
    status: PeerStatus = PeerStatus.APPROVED

    def __post_init__(self) -> None:
        """Run :data:`_PAIRING_VALIDATOR`; re-raise as ``ValueError``."""
        try:
            _PAIRING_VALIDATOR(asdict(self))
        except vol.Invalid as exc:
            field_name = exc.path[0] if exc.path else "<row>"
            raise ValueError(f"StoredPairing.{field_name}: {exc.msg}") from exc


@dataclass
class PairingSummary(DataClassORJSONMixin):
    """
    Public-facing wire view of :class:`StoredPairing`.

    Drops ``static_x25519_pub`` — the raw 32-byte pubkey is
    on-disk only; ``pin_sha256`` is the wire-friendly form
    UIs render. Mirrors the :class:`PeerSummary` projection
    seam on the receiver side so a future "store extra
    offloader-only fields on the row" change can't leak those
    by accident.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    label: str
    paired_at: float
    status: PeerStatus


@dataclass
class RemoteBuildSettings(DataClassORJSONMixin):
    """
    Receiver-side settings for the remote-build feature (storage shape).

    Stored in ``.device-builder.json`` under the ``_remote_build``
    top-level key. Carries the master ``enabled`` toggle and the
    ``manual_hosts`` list (cross-subnet / non-mDNS peers the user
    typed in by hand). APPROVED :class:`StoredPeer` rows used to
    live here under a ``peers`` field; that moved to its own
    per-file :class:`~helpers.storage.Store` at
    ``<config_dir>/.receiver_peers.json`` (mirrors the offloader-
    side :class:`OffloaderRemoteBuildSettings` shape) so reads
    short-circuit through RAM and don't race a write in flight.
    Legacy ``peers`` entries on older sidecars are silently
    ignored at load time. The shape used to also persist a
    ``tokens`` list (issued bearer tokens, hash-only); that field
    was deleted in phase 4a-r2 along with the rest of the dormant
    bearer machinery.
    """

    enabled: bool = False
    manual_hosts: list[ManualHost] = field(default_factory=list)


@dataclass
class RemoteBuildSettingsView(DataClassORJSONMixin):
    """
    Wire view of :class:`RemoteBuildSettings`.

    Returned from every WS command that exposes settings to a
    client. Mirrors :class:`RemoteBuildSettings` plus the
    ``peers`` projection (PENDING + APPROVED merged from the
    controller's in-memory dicts and projected to
    :class:`PeerSummary` so the raw X25519 pubkey bytes never
    reach the wire). The frontend's primary peer surface is the
    ``subscribe_events`` initial-state push + bus events; the
    field is kept here so a client that round-trips
    :meth:`set_settings` / :meth:`get_settings` sees a
    consistent shape with what the snapshot delivered.
    """

    enabled: bool = False
    manual_hosts: list[ManualHost] = field(default_factory=list)
    peers: list[PeerSummary] = field(default_factory=list)


@dataclass
class ReceiverPeers(DataClassORJSONMixin):
    """
    Receiver-side APPROVED peers (storage shape).

    Stored in its own per-file :class:`~helpers.storage.Store`
    instance at ``<config_dir>/.receiver_peers.json`` — sibling
    of the metadata sidecar rather than a sub-key of it, so
    atomic writes are per-domain (corrupting the peers file
    can't take out the rest of ``.device-builder.json``) and a
    receiver-only mutation doesn't have to acquire the metadata
    transaction lock. Mirrors the offloader-side
    :class:`OffloaderRemoteBuildSettings` shape exactly: one
    ``StoredPeer`` list, no other fields, RAM-canonical at
    runtime.

    PENDING peers live in ``RemoteBuildController._pending_peers``
    and are never persisted (their lifetime is bounded by the
    pairing window). Only APPROVED rows reach this file.
    """

    peers: list[StoredPeer] = field(default_factory=list)


@dataclass
class OffloaderRemoteBuildSettings(DataClassORJSONMixin):
    """
    Offloader-side settings for the remote-build feature (storage shape).

    Stored in its own per-file :class:`~helpers.storage.Store`
    instance at ``<config_dir>/.offloader_pairings.json`` —
    sibling of the metadata sidecar rather than a sub-key of it,
    so atomic writes are per-domain (corrupting the offloader
    pairings file can't take out the receiver-side
    ``.device-builder.json``) and there's no lock contention
    against unrelated metadata writers. A dashboard playing both
    roles persists each side's state independently; a future
    "split offloader / receiver into separate processes" refactor
    only has to move one file.

    ``pairings`` carries phase-4a-o :class:`StoredPairing`
    rows: the offloader's pinned receivers. There's no
    ``enabled`` toggle here because the offloader is always
    the initiator (it doesn't bind a listener); whether the
    dashboard exposes the offloader UI is a frontend concern
    for now.
    """

    pairings: list[StoredPairing] = field(default_factory=list)


@dataclass
class OffloaderRemoteBuildSettingsView(DataClassORJSONMixin):
    """
    Wire view of :class:`OffloaderRemoteBuildSettings`.

    Returned from offloader-side WS commands. ``pairings`` is
    projected to :class:`PairingSummary` (drops
    ``static_x25519_pub``); same projection-seam pattern as
    :class:`RemoteBuildSettingsView`.
    """

    pairings: list[PairingSummary] = field(default_factory=list)


@dataclass
class PairingWindowState(DataClassORJSONMixin):
    """
    In-process pairing-window state on the receiver.

    The pairing window narrows when ``intent="pair_request"``
    Noise frames are even accepted: only while the receiver-side
    Pairing requests screen is mounted. ``open`` is the boolean
    state; ``expires_in_seconds`` is the remaining lifetime when
    the window is open (``None`` when closed). The frontend
    renders a live countdown from ``expires_in_seconds`` and
    re-extends by calling ``remote_build/set_pairing_window``
    with ``open=true`` on each activity-driven extend tick (one
    call per 30s on the wire).

    Wire shape for the ``set_pairing_window`` response and the
    ``remote_build_pairing_window_changed`` event payload. Not
    persisted; the per-client extend timestamps live in
    :attr:`RemoteBuildController._pairing_window_clients` and the
    auto-close timer in
    :attr:`RemoteBuildController._pairing_window_handle`. State
    resets on every dashboard restart (which is fine; the
    receiving dashboard's user re-opens the Pairing requests
    screen after restart and the window opens fresh).
    """

    open: bool
    expires_in_seconds: float | None = None


@dataclass
class RemoteBuildPeer(DataClassORJSONMixin):
    """
    A peer dashboard known to this dashboard.

    Wire shape returned from ``remote_build/list_hosts``. Two
    sources land in the same row shape:

    * ``source=MDNS``: discovered via the
      ``_esphomebuilder._tcp.local.`` browse. ``name`` is the
      mDNS service-instance name (leftmost label, e.g.
      ``desktop``); ``hostname`` is the SRV target (e.g.
      ``desktop.local.``); ``addresses`` is the parsed A / AAAA
      list with IPv6 scope preserved; versions come from TXT.
    * ``source=MANUAL``: user-supplied via
      ``remote_build/add_manual_host``. ``name`` is the full
      hostname verbatim (NOT the leftmost label) so an IP-only
      entry like ``192.168.1.10`` reads sensibly in the UI rather
      than truncating to ``"192"``. ``hostname`` is the same
      user-entered string, ``port`` is the user-entered port,
      ``addresses`` is empty, and version fields are blank until
      phase 4 attempts the connection.

    Phase 2 stops at discovery + manual entry; pairing / connection
    / fingerprint pinning lands in later phases.
    """

    name: str
    hostname: str
    port: int
    source: RemoteBuildPeerSource
    addresses: list[str] = field(default_factory=list)
    server_version: str = ""
    esphome_version: str = ""


@dataclass
class IdentityView(DataClassORJSONMixin):
    """
    Receiver-side dashboard identity, projected for the Settings UI.

    Returned from ``remote_build/get_identity`` and
    ``remote_build/rotate_identity``. The cert + key PEMs are
    intentionally NOT included: only the ``pin_sha256`` (the
    SHA-256 of the cert's SubjectPublicKeyInfo, lowercase hex) is
    safe to ship, and the cert PEM itself adds nothing the
    fingerprint doesn't already let an offloader pin against.

    ``server_version`` is this dashboard's package version;
    ``esphome_version`` is the bundled esphome's. Both are also
    advertised in mDNS TXT (see :class:`DashboardAdvertiser`),
    but the Settings UI doesn't browse mDNS to render its own
    "Build host" card — surfacing them here keeps the card a
    single WS call.

    ``listener_bound`` reports whether the
    peer-link Noise WS listener is currently
    serving traffic on this dashboard. Lets the Settings UI
    distinguish "rotation succeeded AND the listener is back
    up" from "rotation succeeded but the rebuild fail-softed"
    (port now bound by something else, cert load throws, …).
    The latter is silent in the logs without this flag.
    """

    dashboard_id: str
    pin_sha256: str
    server_version: str
    esphome_version: str
    listener_bound: bool = False
