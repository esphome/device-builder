"""Receiver-side remote-build storage shapes + wire views."""

from __future__ import annotations

from dataclasses import dataclass, field

from mashumaro.mixins.orjson import DataClassORJSONMixin

from .enums import PeerStatus


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

    ``dashboard_id`` is the offloader's stable identity; sent
    in the pair_request payload so the admin UI has a friendly
    identifier (the X25519 pubkey alone doesn't carry one).
    Primary key for the receiver WS surface
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

    ``connected`` reports whether the receiver currently has
    an active peer-link session for this peer
    (``dashboard_id`` membership in
    :attr:`ReceiverController._peer_link_sessions`). The
    field is computed at snapshot-build time from the
    receiver's RAM-canonical session registry — not stored
    on disk — and live updates flow through the
    :attr:`EventType.RECEIVER_PEER_LINK_SESSION_OPENED` /
    ``_CLOSED`` bus events so a tab subscribing AFTER an
    open / close still sees current state from the snapshot.
    Always ``False`` for PENDING peers: peer-link is gated on
    APPROVED status (the receiver's
    :meth:`ReceiverController.lookup_peer_for_session`
    only returns ``OK`` for APPROVED rows), so a PENDING peer
    can never have a registered session.
    """

    dashboard_id: str
    pin_sha256: str
    label: str
    paired_at: float
    status: PeerStatus
    peer_ip: str = ""
    connected: bool = False


# Default + bounds for :attr:`RemoteBuildSettings.cleanup_ttl_seconds`.
# 24h matches 6c's "subtree is cold if it hasn't been
# submitted-to for a day" intuition. Bounds: 1h floor keeps the
# operator from setting "delete everything every sweep tick" by
# accident; 30d ceiling keeps the cap somewhere finite for the
# input validator (the disk-walk doesn't care about the upper
# bound, but a typed cap surfaces silly inputs at the WS layer
# rather than landing them on disk).
DEFAULT_CLEANUP_TTL_SECONDS = 24 * 60 * 60
MIN_CLEANUP_TTL_SECONDS = 60 * 60
MAX_CLEANUP_TTL_SECONDS = 30 * 24 * 60 * 60


@dataclass
class RemoteBuildSettings(DataClassORJSONMixin):
    """
    Receiver-side settings for the remote-build feature (storage shape).

    Stored in ``.device-builder.json`` under the ``_remote_build``
    top-level key. Carries the master ``enabled`` toggle and the
    TTL sweep's ``cleanup_ttl_seconds`` knob. APPROVED
    :class:`StoredPeer` rows live in their own per-file
    :class:`~helpers.storage.Store` at
    ``<config_dir>/.receiver_peers.json`` (mirrors the offloader-
    side :class:`OffloaderRemoteBuildSettings` shape) so reads
    short-circuit through RAM and don't race a write in flight.
    Legacy ``peers``, ``manual_hosts``, and ``tokens`` entries on
    older sidecars are silently ignored at load time — the
    ``manual_hosts`` flow was removed once the pair dialog started
    typing hostnames straight into ``request_pair``, and the
    ``tokens`` list (hash-only bearer tokens) went with the
    pre-Noise bearer machinery.

    ``enabled`` is the master gate the dashboard checks before
    binding the receiver site. Defaults to ``True`` so fresh
    installs are LAN-discoverable and pairable without an
    extra operator step — the feature's discoverability is the
    point, and the actual privilege grant happens at the
    receiver-side **pair-approval dialog** (a paired peer can
    submit jobs, but pairing requires explicit user approval
    on this dashboard, so binding the port alone grants
    nothing).

    HA-addon deployments override this default at the bind
    site: a fresh addon install with no persisted
    ``_remote_build`` block in metadata does not bind. The
    addon's docker container doesn't expose port 6055 to the
    LAN by default, so binding it would waste the port and
    confuse operators. HA-addon operators who DO want the
    feature (e.g. they've added ``6055/tcp`` to the addon's
    ``ports:`` config, as some legacy-dashboard operators have
    historically done with the HTTP port) flip the toggle in
    Settings; the resulting write persists the block, the
    "explicit operator opt-in" signal flips, and the next
    boot's bind site respects the persisted ``enabled`` field
    exactly like every other deployment mode. See
    :meth:`device_builder.DeviceBuilder._maybe_start_remote_build_site`
    for the gate.

    ``cleanup_ttl_seconds`` is the operator-tunable threshold
    the 6c background sweep uses to decide a remote-build
    subtree is cold enough to delete. Defaults to 24h
    (:data:`DEFAULT_CLEANUP_TTL_SECONDS`); the WS validator
    caps the input between :data:`MIN_CLEANUP_TTL_SECONDS` and
    :data:`MAX_CLEANUP_TTL_SECONDS` so a fat-fingered or
    malicious write can't push the sweep to "delete everything
    every tick" or "never reclaim disk". Missing on an older
    sidecar deserialises to the default via the dataclass
    field default.
    """

    enabled: bool = True
    cleanup_ttl_seconds: int = DEFAULT_CLEANUP_TTL_SECONDS

    def __post_init__(self) -> None:
        """Coerce + clamp ``cleanup_ttl_seconds`` on load.

        The WS validator on :meth:`ReceiverController.set_settings`
        gates writes that come through the WS surface, but the
        on-disk decode path (``from_dict`` →
        ``RemoteBuildSettings(...)``) doesn't apply the same
        ``not_bool`` / range check. A hand-edited or corrupt
        sidecar with ``cleanup_ttl_seconds: true`` would
        deserialise as ``1`` (bool is an int subclass), and
        the sweep would treat anything older than 1s as cold —
        near-immediate cache deletion. Other wrong types (string,
        float, None) would propagate to the sweep's ``now -
        ttl_seconds`` arithmetic and raise ``TypeError``, which
        the controller's cleanup loop catches but logs every
        cycle.

        Both failure modes resolve to the safe default: coerce
        non-int / bool values back to
        :data:`DEFAULT_CLEANUP_TTL_SECONDS` and clamp the
        result to [:data:`MIN_CLEANUP_TTL_SECONDS`,
        :data:`MAX_CLEANUP_TTL_SECONDS`]. The ``enabled``
        toggle is left alone — a bad cleanup TTL shouldn't
        flip the master switch.

        Doesn't reject the row (no ``ValueError``) so the
        load path stays robust against partially-corrupt
        sidecars; the operator's last good ``enabled`` value
        survives even if the TTL field is broken.
        """
        if isinstance(self.cleanup_ttl_seconds, bool) or not isinstance(
            self.cleanup_ttl_seconds, int
        ):
            self.cleanup_ttl_seconds = DEFAULT_CLEANUP_TTL_SECONDS
            return
        if self.cleanup_ttl_seconds < MIN_CLEANUP_TTL_SECONDS:
            self.cleanup_ttl_seconds = MIN_CLEANUP_TTL_SECONDS
        elif self.cleanup_ttl_seconds > MAX_CLEANUP_TTL_SECONDS:
            self.cleanup_ttl_seconds = MAX_CLEANUP_TTL_SECONDS


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

    enabled: bool = True
    cleanup_ttl_seconds: int = DEFAULT_CLEANUP_TTL_SECONDS
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

    PENDING peers live in ``ReceiverController._pending_peers``
    and are never persisted (their lifetime is bounded by the
    pairing window). Only APPROVED rows reach this file.
    """

    peers: list[StoredPeer] = field(default_factory=list)


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
    :attr:`ReceiverController._pairing_window_clients` and the
    auto-close timer in
    :attr:`ReceiverController._pairing_window_handle`. State
    resets on every dashboard restart (which is fine; the
    receiving dashboard's user re-opens the Pairing requests
    screen after restart and the window opens fresh).
    """

    open: bool
    expires_in_seconds: float | None = None


@dataclass
class IdentityView(DataClassORJSONMixin):
    """
    Receiver-side dashboard identity, projected for the Settings UI.

    Returned from ``remote_build/get_identity`` and
    ``remote_build/rotate_identity``. The X25519 private key
    is intentionally NOT included: only the ``pin_sha256`` (the
    SHA-256 of the X25519 public key, lowercase hex) is safe to
    ship, and the pubkey itself adds nothing the fingerprint
    doesn't already let an offloader pin against.

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
