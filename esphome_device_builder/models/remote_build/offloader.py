"""Offloader-side remote-build storage shapes + discovered-host wire view."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import voluptuous as vol
from mashumaro.mixins.orjson import DataClassORJSONMixin

from ...helpers.voluptuous_validators import lowercase_hex, not_bool
from .enums import PeerStatus, RemoteBuildPeerSource

# Cap on :attr:`StoredPairing.esphome_version`. The wire-extract
# path on the offloader applies the same cap before writing so a
# malicious / buggy receiver can't poison the sidecar with a
# multi-MB string. 64 chars is generous for any real
# ``esphome.const.__version__`` (``"2026.5.0-dev"`` is 13).
PAIRING_VERSION_MAX_LEN = 64

# Defense-in-depth at the storage seam: the same trust surface
# as anything else under ``<config_dir>``, and ``from_dict``
# round-trips malformed rows regardless of size otherwise.
#
# **Maintenance note.** Second source of truth alongside the
# dataclass annotations — keep them in lockstep. Add / rename /
# remove the field AND the schema entry in the same change, or
# load-time validation silently desyncs.
#
# **No comparable schema on :class:`StoredPeer`.** The
# receiver-side write path is constructively narrow (Noise XX
# pins ``static_x25519_pub`` at 32 bytes; the dispatcher
# validates ``dashboard_id``; ``_normalize_label`` caps
# ``label``); the offloader-side ``request_pair`` reaches the
# controller through fewer upstream gates, so a fail-closed
# disk shape is more valuable here. Disk-corruption on
# ``StoredPeer`` is caught by the load-path's fail-closed
# ``except Exception`` reset.
_PAIRING_VALIDATOR = vol.Schema(
    {
        # RFC 1035 §2.3.4 caps a FQDN at 253; round up to 255
        # for trailing-dot variations. ``\S`` rejects
        # whitespace-only that would pass ``Length(min=1)``.
        vol.Required("receiver_hostname"): vol.All(
            str, vol.Length(min=1, max=255), vol.Match(r"\S")
        ),
        # ``not_bool`` first — voluptuous's ``int`` accepts
        # ``bool`` (Python's ``isinstance(True, int)`` is true),
        # so ``receiver_port=True`` would pass as port 1.
        vol.Required("receiver_port"): vol.All(not_bool, int, vol.Range(min=1, max=65535)),
        vol.Required("pin_sha256"): lowercase_hex(64),
        # Raw X25519 pubkey — exactly 32 bytes per RFC 7748 §5.
        vol.Required("static_x25519_pub"): vol.All(bytes, vol.Length(min=32, max=32)),
        vol.Required("label"): vol.All(str, vol.Length(max=128)),
        # ``vol.All(not_bool, ...)`` not ``vol.Any(int, float,
        # not_bool)`` — ``Any`` short-circuits on the first
        # accepting branch and ``int`` would accept ``True``.
        vol.Required("paired_at"): vol.All(not_bool, vol.Any(int, float)),
        # ``vol.In(PeerStatus)`` matches both enum instance and
        # bare string forms (``"pending"`` / ``"approved"``)
        # that ``DataClassORJSONMixin.from_dict`` produces.
        # ``__post_init__`` runs the validator over
        # ``asdict(self)``, which includes ``status``, so the
        # schema must accept it even though disk shape is
        # APPROVED-only.
        vol.Required("status"): vol.In(PeerStatus),
        # Empty when no peer-link session has opened yet (fresh
        # PENDING row, or an older sidecar predating this field).
        vol.Required("esphome_version"): vol.All(str, vol.Length(max=PAIRING_VERSION_MAX_LEN)),
        # Strict ``bool`` accept — no ``not_bool`` wrapping,
        # so an ``int``-coerced-to-bool gets rejected (same
        # shape that bit ``cleanup_ttl_seconds``).
        vol.Required("enabled"): bool,
    }
)


@dataclass
class StoredPairing(DataClassORJSONMixin):
    """
    Offloader-side record of a paired (or pending) receiver.

    Persisted in a per-file ``Store`` at
    ``<config_dir>/.offloader_pairings.json``; RAM-first model
    (the controller's ``_pairings`` dict is the source of truth,
    debounced to disk).

    Keys on ``(receiver_hostname, receiver_port)`` because
    that's what the user enters in the Pair dialog and what
    reconnection needs — *not* the receiver-side ``StoredPeer``
    key (which is the offloader's ``dashboard_id``). The two
    shapes are deliberately not the same row passed both ways.

    ``static_x25519_pub`` is the canonical identifier the Noise
    handshake binds to; ``pin_sha256`` is its lowercase-hex
    SHA-256 (the OOB-verified pin). Both are stored so a future
    re-pair can detect identity rotation without re-deriving on
    every connect.

    ``status`` defaults to APPROVED because the disk filter
    strips PENDING rows before write — reading a row back from
    the ``Store`` always produces APPROVED. PENDING is the
    explicit-construction case: ``request_pair`` sets it before
    adding to the dict; ``_apply_pair_status_result`` flips it
    to APPROVED in place when the receiver reports the flip.

    ``__post_init__`` runs :data:`_PAIRING_VALIDATOR` so a
    malformed sidecar row (hand-edit, partial-write recovery,
    schema-skew across upgrades) is rejected by ``from_dict``
    rather than landing as a multi-megabyte string in memory.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    static_x25519_pub: bytes
    label: str
    paired_at: float
    status: PeerStatus = PeerStatus.APPROVED
    # Receiver-advertised ``esphome.const.__version__`` captured
    # on every peer-link session-open. Empty on a fresh PENDING
    # row or an older sidecar. Refreshes on every reconnect so a
    # receiver upgrade picks up on the next session-open without
    # operator action; the persisted copy is the cross-restart
    # fallback for the UI's "last known: X.Y.Z" display.
    esphome_version: str = ""
    # Per-pairing master toggle. ``False`` skips the row in
    # :func:`helpers.build_scheduler.pick_build_path` — the
    # operator wants the receiver paired (peer-link stays open,
    # Send-builds still works) but doesn't want transparent
    # install to route here. Older sidecars deserialise as
    # ``True`` (the historical implicit behaviour).
    enabled: bool = True

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
    on-disk only; ``pin_sha256`` is the wire-friendly form.

    ``connected`` / ``connecting`` form a tri-state with
    permanently-orphaned (pin mismatch, superseded — both
    ``False``). UI renders "Connected" / "Connecting…" /
    "Disconnected (last error: …)" from the three fields;
    orphan's only recovery is re-pair or unpair.

    ``last_connect_error`` is ``"{ExcType}: {msg}"`` for
    transport / Noise errors, ``"auth rejected"`` for
    handshake-rejected sessions, ``"pin mismatch"`` for the
    orphan-on-rotation path. Cleared on every successful
    session-open; empty on never-connected rows.
    """

    receiver_hostname: str
    receiver_port: int
    pin_sha256: str
    label: str
    paired_at: float
    status: PeerStatus
    connected: bool = False
    connecting: bool = False
    last_connect_error: str = ""
    # Refreshed on every peer-link session-open; empty on a
    # never-connected row / older sidecar. Surfaced as
    # "last known: X.Y.Z" so the operator can spot a version
    # skew before pick_build_path silently falls back to LOCAL.
    esphome_version: str = ""
    # Per-pairing enable toggle the Settings UI renders the
    # switch from; ``False`` skips the row in pick_build_path.
    enabled: bool = True


@dataclass
class OffloaderRemoteBuildSettings(DataClassORJSONMixin):
    """
    Offloader-side settings for the remote-build feature (storage shape).

    Stored in its own per-file ``Store`` at
    ``<config_dir>/.offloader_pairings.json`` — sibling of the
    metadata sidecar so atomic writes are per-domain (corrupting
    pairings can't take out ``.device-builder.json``) and a
    future "split offloader / receiver into separate processes"
    refactor only has to move one file.

    ``remote_builds_enabled=False`` short-circuits the
    transparent install flow to LOCAL for every device. Paired
    receivers stay paired (peer-link sessions stay open;
    Send-builds power-user dialog still works); only the
    implicit "Install → maybe route to a receiver" path is
    gated off.
    """

    pairings: list[StoredPairing] = field(default_factory=list)
    remote_builds_enabled: bool = True


@dataclass
class OffloaderRemoteBuildSettingsView(DataClassORJSONMixin):
    """
    Wire view of :class:`OffloaderRemoteBuildSettings`.

    ``pairings`` is projected to :class:`PairingSummary` (drops
    ``static_x25519_pub``).
    """

    pairings: list[PairingSummary] = field(default_factory=list)
    remote_builds_enabled: bool = True


@dataclass
class RemoteBuildPeer(DataClassORJSONMixin):
    """
    A peer dashboard known to this dashboard.

    Wire shape reaching the frontend through
    :meth:`OffloaderController.hosts_snapshot` (the sync read
    used by ``subscribe_events.initial_state.hosts``) plus the
    matching ``REMOTE_BUILD_HOST_ADDED`` /
    ``REMOTE_BUILD_HOST_REMOVED`` events. The only source today
    is ``source="mdns"``: ``name`` is the mDNS service-instance
    name; ``hostname`` is the SRV target; ``addresses`` is the
    parsed A / AAAA list with IPv6 scope preserved; versions
    come from TXT.
    """

    name: str
    hostname: str
    port: int
    source: RemoteBuildPeerSource
    addresses: list[str] = field(default_factory=list)
    server_version: str = ""
    esphome_version: str = ""
    # Receiver's peer-link X25519 pubkey hash + port, pulled
    # from the ``_esphomebuilder._tcp.local.`` TXT record.
    # Lets the offloader match a discovered broadcast against a
    # stored pairing's ``pin_sha256`` and dial the right peer-
    # link port for the auto-rebind probe. Empty string / 0
    # for receivers that haven't bound the peer-link listener
    # (default-off mode).
    pin_sha256: str = ""
    remote_build_port: int = 0
