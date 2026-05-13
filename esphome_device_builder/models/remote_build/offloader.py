"""Offloader-side remote-build storage shapes + discovered-host wire view."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import voluptuous as vol
from mashumaro.mixins.orjson import DataClassORJSONMixin

from ...helpers.voluptuous_validators import lowercase_hex, not_bool
from .enums import PeerStatus, RemoteBuildPeerSource

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
# **No comparable schema on :class:`StoredPeer`.** Both rows
# have a disk-reconstruction path (``_decode_pairings`` /
# ``_decode_peers`` call ``from_dict`` on controller start),
# so the validator gap on :class:`StoredPeer` is real — a
# range-violating value (negative port, non-hex pin, etc.)
# could survive into runtime if it landed on disk. We accept
# the gap because:
#
# 1. The fail-closed-on-corruption posture both ``_decode_*``
#    functions take catches the load-bearing failure mode.
#    A malformed JSON blob or a type mismatch mashumaro can't
#    coerce raises out of ``from_dict``; the outer
#    ``except Exception`` resets the store to empty rather
#    than crashing dashboard startup. ``StoredPeer``'s
#    dataclass type annotations get most of that for free
#    via mashumaro.
# 2. The receiver-side row is constructively narrow: every
#    field a schema would validate is gated upstream at
#    *write* time (Noise XX pins ``static_x25519_pub`` at
#    32 bytes; the dispatcher validates ``dashboard_id``
#    against ``DASHBOARD_ID_PATTERN``; ``_normalize_label``
#    caps ``label`` before ``record_pair_request`` is
#    called). A corrupt disk row would have to come from
#    *us* writing bad data, not from a malicious peer.
# 3. The defense-in-depth a schema would add (catching
#    logically-invalid-but-type-compatible disk corruption
#    on data we wrote) is lower-value than the maintenance
#    cost of a second source of truth alongside the
#    dataclass annotations.
#
# Cap on :attr:`StoredPairing.esphome_version`. The validator
# below rejects rows whose stored version exceeds this length;
# the wire-extract path on the offloader side
# (:func:`controllers.remote_build.peer_link_client._extract_receiver_esphome_version`)
# applies the same cap before writing the field so a malicious
# / buggy receiver can't poison the sidecar with a multi-MB
# string that then fails to load on the next start. 64 chars is
# generous for any real ``esphome.const.__version__``
# (``"2026.5.0-dev"`` is 13 chars) — the cap is the "this isn't
# a version string anymore" boundary, not a tight fit.
PAIRING_VERSION_MAX_LEN = 64

# :class:`StoredPairing` carries a schema because its
# offloader-side write path is broader (user-controlled
# ``request_pair`` args reach the controller through fewer
# upstream gates than the receiver-side equivalents) and a
# fail-closed disk shape is more valuable there. The
# asymmetry is the honest reflection of the asymmetric
# write-side trust, not an outstanding bug.
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
        # can be reused by the 4a-o WS-command validators without
        # drifting.
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
        # Receiver's ``esphome.const.__version__`` captured at
        # peer-link session-open time. In-RAM only at session
        # close; persisted across restarts so a disconnected row
        # still carries its last-seen version for the UI's
        # "last known: X.Y.Z" display. Capped to keep a corrupt
        # / hand-edited sidecar from landing a megabyte string.
        # Empty when no peer-link session has opened yet (fresh
        # PENDING row, or an old sidecar from before this field
        # existed); pick_build_path's version-compat gate
        # accepts empty as "unknown, fall through to compat".
        vol.Required("esphome_version"): vol.All(str, vol.Length(max=PAIRING_VERSION_MAX_LEN)),
        # Per-pairing master toggle. The Settings UI exposes
        # one switch per paired build server; when
        # ``False`` the scheduler skips this row entirely (the
        # operator wants this receiver paired but doesn't want
        # transparent install to route here). ``not_bool`` not
        # needed — ``bool`` is the strict accept, and an
        # explicit ``bool`` requirement rejects the same
        # ``int``-coerced-to-bool shape that bit the
        # cleanup_ttl_seconds validator.
        vol.Required("enabled"): bool,
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
    # Receiver-advertised ``esphome.const.__version__``, captured
    # on every peer-link session-open from the ``intent_response``
    # post-handshake payload. Empty on a fresh PENDING row + on
    # APPROVED rows loaded from an older sidecar that predates
    # this field (``DataClassORJSONMixin.from_dict`` tolerates
    # missing fields with declared dataclass defaults; the empty
    # string default below is what fills in). The version
    # refreshes on every reconnect — pick_build_path consumes
    # the in-RAM value so a receiver upgrade picks up on the
    # next session-open without operator action; the persisted
    # copy is the cross-restart fallback for "last known
    # version" UI display.
    esphome_version: str = ""
    # 7b per-pairing master toggle. ``True`` matches the
    # historical implicit behaviour (every APPROVED row is
    # eligible for the scheduler), so older sidecars from
    # before this field landed deserialise as enabled.
    # When ``False`` the scheduler's
    # :func:`helpers.build_scheduler.pick_build_path` skips
    # this row — the operator wants the receiver paired
    # (peer-link stays open, Send-builds power-user surface
    # still works) but doesn't want transparent install to
    # route here.
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
    on-disk only; ``pin_sha256`` is the wire-friendly form
    UIs render. Mirrors the :class:`PeerSummary` projection
    seam on the receiver side so a future "store extra
    offloader-only fields on the row" change can't leak those
    by accident.

    ``connected`` is the offloader-side mirror of
    :attr:`PeerSummary.connected`: it reports whether the
    offloader's per-pairing :class:`PeerLinkClient` task
    currently has an open peer-link session against the
    receiver. Computed at snapshot-build time from
    :attr:`OffloaderController._open_peer_links`
    (a ``set[str]`` of ``pin_sha256`` values, populated by
    listeners on :attr:`EventType.OFFLOADER_PEER_LINK_OPENED` /
    :attr:`EventType.OFFLOADER_PEER_LINK_CLOSED` that
    :class:`PeerLinkClient` already fires from
    :meth:`_fire_opened` / :meth:`_fire_closed`). Always
    ``False`` for PENDING rows — the offloader doesn't spawn
    a peer-link client until the receiver flips the row to
    APPROVED.

    ``connecting`` is the "live but not connected" state: the
    per-pairing client task is alive (not orphaned) and not
    currently sitting on an open session. The reconnect-backoff
    loop in :meth:`PeerLinkClient.run` cycles through this
    state every time a session ends and the client is about to
    retry. UI uses it to render a "Connecting…" indicator
    distinct from a permanently-orphaned pairing (pin mismatch,
    superseded) where ``connecting`` is ``False`` *and*
    ``connected`` is ``False`` — the operator's recovery there
    is re-pair / unpair, not "wait for reconnect."

    ``last_connect_error`` is a one-line human-readable
    description of the most recent connection failure
    (``"<ExceptionType>: <message>"`` form for transport /
    Noise errors, ``"auth rejected"`` for handshake-rejected
    sessions, ``"pin mismatch"`` for the orphan-on-rotation
    path). Cleared when a session reaches the post-handshake
    open state. Empty on a never-connected pairing where the
    client task hasn't completed its first attempt yet.
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
    # Receiver-advertised ``esphome.const.__version__``, mirroring
    # :attr:`StoredPairing.esphome_version`. Refreshed on every
    # peer-link session-open; empty on a never-connected row /
    # an older sidecar. Surfaced in the offloader's paired-
    # receivers UI as a "last known: X.Y.Z" line so the operator
    # can spot a version skew before the version-compat gate in
    # pick_build_path silent-fallbacks them to LOCAL.
    esphome_version: str = ""
    # 7b per-pairing enable toggle, mirroring
    # :attr:`StoredPairing.enabled`. The 7b Settings UI
    # renders the switch from this field.
    enabled: bool = True


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

    ``pairings`` carries :class:`StoredPairing` rows: the
    offloader's pinned receivers.

    ``remote_builds_enabled`` is the master switch the
    scheduler reads. When ``False`` the transparent install
    flow short-circuits to LOCAL for every device — paired
    receivers stay paired and the peer-link sessions stay
    open (the explicit Send-builds power-user dialog still
    works); only the implicit "Install → maybe route to a
    receiver" path is gated off. Default ``True`` matches
    the implicit behaviour the dashboard had before this
    field existed: any APPROVED + connected + idle pairing
    was eligible. Older sidecars from before the field
    landed deserialise as enabled.
    """

    pairings: list[StoredPairing] = field(default_factory=list)
    remote_builds_enabled: bool = True


@dataclass
class OffloaderRemoteBuildSettingsView(DataClassORJSONMixin):
    """
    Wire view of :class:`OffloaderRemoteBuildSettings`.

    Returned from offloader-side WS commands. ``pairings`` is
    projected to :class:`PairingSummary` (drops
    ``static_x25519_pub``); same projection-seam pattern as
    :class:`RemoteBuildSettingsView`.

    ``remote_builds_enabled`` mirrors the storage-shape
    master toggle so the frontend's "Remote builds enabled"
    switch can read its initial state from the same
    ``get_offloader_settings`` round-trip that already
    surfaces the pairings list.
    """

    pairings: list[PairingSummary] = field(default_factory=list)
    remote_builds_enabled: bool = True


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
      a pairing attempt runs the connection.
    """

    name: str
    hostname: str
    port: int
    source: RemoteBuildPeerSource
    addresses: list[str] = field(default_factory=list)
    server_version: str = ""
    esphome_version: str = ""
    # Receiver's peer-link X25519 static pubkey hash (lowercase-
    # hex SHA-256, the same value as
    # :attr:`StoredPairing.pin_sha256`) and peer-link Noise WS
    # port, both pulled out of the
    # ``_esphomebuilder._tcp.local.`` TXT record. The peer-link
    # identity is the dashboard's X25519 keypair persisted at
    # ``<config_dir>/.device-builder-peer-link-key.bin`` (see
    # ``helpers/peer_link_identity.py``). The offloader uses
    # both to match a discovered broadcast against a stored
    # pairing's ``pin_sha256`` and dial the right peer-link
    # port for the auto-rebind probe. Empty string / 0 for:
    # receivers that haven't bound the
    # peer-link listener (default-off mode), and ``MANUAL``
    # rows (which never go through the mDNS resolve path; the
    # user typed the hostname/port and the pair flow captures
    # the pin into ``StoredPairing`` rather than back onto this
    # row).
    pin_sha256: str = ""
    remote_build_port: int = 0
