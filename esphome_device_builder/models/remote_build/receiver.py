"""Receiver-side remote-build storage shapes + wire views."""

from __future__ import annotations

from dataclasses import dataclass, field

from mashumaro.mixins.orjson import DataClassORJSONMixin

from .enums import PeerStatus


@dataclass
class StoredPeer(DataClassORJSONMixin):
    """
    Receiver-side record of a paired (or pending) offloader.

    APPROVED rows are persisted in a per-file
    :class:`~helpers.storage.Store` at
    ``<config_dir>/.receiver_peers.json`` (wrapped by
    :class:`ReceiverPeers`). PENDING rows live only in
    ``ReceiverController._pending_peers`` and never reach disk —
    their lifetime is bounded by the pairing window.

    ``static_x25519_pub`` is the canonical identifier the Noise
    handshake binds to; ``pin_sha256`` is its lowercase-hex
    SHA-256 (the wire-friendly form used in log lines / event
    payloads). ``dashboard_id`` is the offloader's stable identity
    and the primary key for the WS surface, so a future X25519
    keypair rotation on the offloader doesn't change the
    user-facing handle. ``peer_ip`` is empty string when unknown
    — legacy rows from receivers that pre-date this field load
    cleanly with the default.
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

        ``dashboard_id`` (the row's primary key) is intentionally
        left out of the refresh set. Caller is responsible for the
        no-demote-when-APPROVED check before invoking — see
        ``record_pair_request`` for the gating logic. (PENDING vs
        APPROVED is tracked outside this row: PENDING rows live
        in ``ReceiverController._pending_peers``, APPROVED in
        ``ReceiverPeers.peers``.)
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
    on-disk only; ``pin_sha256`` is the wire-friendly form.
    ``connected`` is computed at snapshot-build time from the
    receiver's RAM-canonical session registry and is always
    ``False`` for PENDING peers (peer-link is APPROVED-gated).
    Live updates flow through ``RECEIVER_PEER_LINK_SESSION_OPENED``
    / ``RECEIVER_PEER_LINK_SESSION_CLOSED`` events, so a tab
    subscribing AFTER an open / close still sees current state
    from the snapshot.
    """

    dashboard_id: str
    pin_sha256: str
    label: str
    paired_at: float
    status: PeerStatus
    peer_ip: str = ""
    connected: bool = False


# 24h default cold-window for the 6c cleanup sweep. 1h floor
# keeps the operator from setting "delete every tick" by
# accident; 30d ceiling keeps a fat-fingered input finite.
DEFAULT_CLEANUP_TTL_SECONDS = 24 * 60 * 60
MIN_CLEANUP_TTL_SECONDS = 60 * 60
MAX_CLEANUP_TTL_SECONDS = 30 * 24 * 60 * 60


@dataclass
class RemoteBuildSettings(DataClassORJSONMixin):
    """
    Receiver-side settings for the remote-build feature (storage shape).

    Stored in ``.device-builder.json`` under the ``_remote_build``
    key. Carries the master ``enabled`` toggle + the TTL sweep
    knob; APPROVED :class:`StoredPeer` rows live in their own
    per-file :class:`~helpers.storage.Store` at
    ``<config_dir>/.receiver_peers.json``. Legacy ``peers``,
    ``manual_hosts``, and ``tokens`` entries on older sidecars
    are silently ignored at load time.

    ``enabled`` defaults to ``True`` so fresh installs are
    LAN-discoverable + pairable without an extra operator step
    — binding the port grants no privilege by itself; pair-
    approval is what authorises a peer. **HA-addon deployments
    override this default at the bind site**: a fresh addon
    install with no persisted ``_remote_build`` block does not
    bind, since the addon container doesn't expose port 6055 by
    default. Operators who add ``6055/tcp`` to the addon's
    ``ports:`` flip the toggle in Settings; the persisted block
    then carries explicit opt-in and subsequent boots respect
    ``enabled`` like every other mode. See
    :meth:`_remote_build_lifecycle.RemoteBuildLifecycle.maybe_start`.

    ``cleanup_ttl_seconds`` is bounded between
    :data:`MIN_CLEANUP_TTL_SECONDS` and
    :data:`MAX_CLEANUP_TTL_SECONDS` (see
    :meth:`__post_init__`).
    """

    enabled: bool = True
    cleanup_ttl_seconds: int = DEFAULT_CLEANUP_TTL_SECONDS

    def __post_init__(self) -> None:
        """
        Coerce + clamp ``cleanup_ttl_seconds`` on load.

        WS-validator gates writes, but the from_dict load path
        doesn't — a hand-edited / corrupt sidecar with
        ``cleanup_ttl_seconds: true`` would deserialise as ``1``
        (bool is an int subclass) and trigger near-immediate
        cache deletion. Non-int / bool values reset to
        :data:`DEFAULT_CLEANUP_TTL_SECONDS`; in-range integers
        clamp to [MIN, MAX]. ``enabled`` is left alone — a bad
        TTL shouldn't flip the master switch. Never raises so
        the load path stays robust against partial corruption.
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

    Adds the ``peers`` projection (PENDING + APPROVED merged
    and projected to :class:`PeerSummary` so raw X25519 pubkey
    bytes never reach the wire). Frontend's primary peer
    surface is the ``subscribe_events`` initial-state push +
    bus events; this field exists so settings round-trips see
    a shape consistent with the snapshot.
    """

    enabled: bool = True
    cleanup_ttl_seconds: int = DEFAULT_CLEANUP_TTL_SECONDS
    peers: list[PeerSummary] = field(default_factory=list)


@dataclass
class ReceiverPeers(DataClassORJSONMixin):
    """
    Receiver-side APPROVED peers (storage shape).

    Stored in its own per-file ``Store`` at
    ``<config_dir>/.receiver_peers.json`` — sibling of the
    metadata sidecar, so atomic writes are per-domain and a
    receiver-only mutation doesn't acquire the metadata
    transaction lock. PENDING peers live in RAM only.
    """

    peers: list[StoredPeer] = field(default_factory=list)


@dataclass
class PairingWindowState(DataClassORJSONMixin):
    """
    In-process pairing-window state on the receiver.

    The pairing window narrows when ``intent="pair_request"``
    frames are even accepted: only while the receiver-side
    Pairing-requests screen is mounted. ``expires_in_seconds``
    is ``None`` when ``open`` is ``False``. Not persisted —
    state resets on every dashboard restart.
    """

    open: bool
    expires_in_seconds: float | None = None


@dataclass
class IdentityView(DataClassORJSONMixin):
    """
    Receiver-side dashboard identity, projected for the Settings UI.

    The X25519 private key is intentionally NOT included; only
    ``pin_sha256`` is safe to ship, and the pubkey itself adds
    nothing the fingerprint doesn't already let an offloader
    pin against.

    ``listener_bound`` distinguishes "rotation succeeded AND
    the listener is back up" from "rotation succeeded but the
    rebuild fail-softed" (port now bound by something else,
    cert load throws). The latter is silent in the logs
    without this flag.
    """

    dashboard_id: str
    pin_sha256: str
    server_version: str
    esphome_version: str
    listener_bound: bool = False
