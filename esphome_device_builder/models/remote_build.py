"""Remote-build feature models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, TypedDict

from mashumaro.mixins.orjson import DataClassORJSONMixin


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


@dataclass
class StoredToken(DataClassORJSONMixin):
    """
    A receiver-side issued bearer token, persisted by hash.

    Cleartext is the wire form ``{token_id}.{secret}``; only
    ``secret_sha256`` lands on disk. ``token_id`` is the lookup key
    (constant-time table hit), ``secret_sha256`` is what the
    middleware compares against the bearer's secret half via
    ``hmac.compare_digest``.

    ``bound_dashboard_id`` starts ``None`` and is filled in by the
    phase-3b3 first-use binding the first time an authenticated
    request arrives carrying a peer's ``X-Dashboard-ID``. After
    that, requests presenting the same token but a different
    dashboard_id are rejected as 403.
    """

    token_id: str
    label: str
    secret_sha256: str
    created_at: float
    bound_dashboard_id: str | None = None


@dataclass
class TokenSummary(DataClassORJSONMixin):
    """
    Public-facing token row for ``remote_build/list_tokens``.

    Mirrors :class:`StoredToken` but drops ``secret_sha256``: the
    stored hash isn't sensitive in the same way the cleartext is,
    but exposing it would let a network attacker who's already
    seen the on-disk metadata match candidate cleartext bearers
    against the wire shape, so the frontend has no business
    reading it.
    """

    token_id: str
    label: str
    created_at: float
    bound_dashboard_id: str | None = None


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


class RemoteBuildPairRequestReceivedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIR_REQUEST_RECEIVED``.

    Fired by the peer-link Noise WS handler when a fresh
    ``intent="pair_request"`` arrives during an open pairing
    window. The receiver Settings UI surfaces the row in the
    Pairing requests inbox; ``peer_ip`` lets the operator
    sanity-check the source against expectations before
    OOB-confirming the pin.
    """

    dashboard_id: str
    pin_sha256: str
    label: str
    peer_ip: str


class RemoteBuildPairStatusChangedData(TypedDict):
    """
    Payload for ``EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED``.

    Fired by ``approve_peer`` (``status="approved"``) and by
    ``remove_peer`` for previously-APPROVED rows
    (``status="removed"``). Removing a still-PENDING row is
    rejection-as-cleanup and intentionally does not fire — see
    ``remove_peer`` docstring.
    """

    dashboard_id: str
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
    """

    dashboard_id: str
    pin_sha256: str
    static_x25519_pub: bytes
    label: str
    paired_at: float
    status: PeerStatus = PeerStatus.PENDING

    def refresh_from_pair_request(
        self,
        *,
        pin_sha256: str,
        static_x25519_pub: bytes,
        label: str,
        paired_at: float,
    ) -> None:
        """
        Update the fields a fresh ``intent="pair_request"`` supplies.

        Owns the contract for "what changes on re-pair": the X25519
        pubkey + its hash (offloader rotated their identity), the
        label (renamed dashboard), and the ``paired_at`` timestamp
        (so the receiver-side inbox sorts the most-recent attempt
        first). ``dashboard_id`` is the row's primary key and is
        intentionally left out of the refresh set; ``status`` is
        also left out because pair_request never changes status by
        itself (the receiver-side user's Accept / Reject does, via
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


@dataclass
class PeerSummary(DataClassORJSONMixin):
    """
    Public-facing wire view of :class:`StoredPeer`.

    Drops ``static_x25519_pub`` — the raw 32-byte pubkey is
    on-disk only; ``pin_sha256`` (lowercase-hex SHA-256 of the
    pubkey) is the wire-friendly form that UIs render for
    OOB-verification.
    """

    dashboard_id: str
    pin_sha256: str
    label: str
    paired_at: float
    status: PeerStatus


@dataclass
class RemoteBuildSettings(DataClassORJSONMixin):
    """
    Receiver-side settings for the remote-build feature (storage shape).

    Stored in ``.device-builder.json`` under the ``_remote_build``
    top-level key. ``tokens`` carries :class:`StoredToken` rows
    *with* the ``secret_sha256`` hash; this is the on-disk /
    in-process shape only and MUST NOT be serialised over the
    wire. Use :class:`RemoteBuildSettingsView` (or the
    ``_summarise_token`` projection) for any response that leaves
    the server.

    ``peers`` carries phase-4a :class:`StoredPeer` rows: the
    receiver's pinned offloaders (Noise XX over a dedicated
    peer-link port replaces the bearer flow). ``tokens`` is on
    a deletion path (phase 4a-r2) once the new auth has soaked.
    """

    enabled: bool = False
    manual_hosts: list[ManualHost] = field(default_factory=list)
    tokens: list[StoredToken] = field(default_factory=list)
    peers: list[StoredPeer] = field(default_factory=list)


@dataclass
class RemoteBuildSettingsView(DataClassORJSONMixin):
    """
    Wire view of :class:`RemoteBuildSettings`.

    Returned from every WS command that exposes settings to a
    client. Identical to :class:`RemoteBuildSettings` except
    ``tokens`` is a list of :class:`TokenSummary` (no
    ``secret_sha256``), so issuing or removing tokens via the
    CRUD methods can't accidentally leak the stored hash back to
    the frontend through the response shape. ``peers`` is also
    projected to :class:`PeerSummary` for symmetry — the storage
    and wire shapes happen to match today, but the projection
    seam keeps a future "store extra peer-only fields" change
    from accidentally leaking those.
    """

    enabled: bool = False
    manual_hosts: list[ManualHost] = field(default_factory=list)
    tokens: list[TokenSummary] = field(default_factory=list)
    peers: list[PeerSummary] = field(default_factory=list)


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
    ``/remote-build/v1/*`` HTTPS receiver site is currently
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
