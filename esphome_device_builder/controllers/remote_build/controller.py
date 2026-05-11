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

Pairing model:

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
from collections.abc import Callable, Coroutine, Hashable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass as _dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from esphome.const import __version__ as esphome_version
from yarl import URL
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from ...constants import __version__ as server_version
from ...helpers import dashboard_identity as _dashboard_identity_helper
from ...helpers.api import CommandError, api_command
from ...helpers.build_scheduler import BuildSchedulerInputs
from ...helpers.dashboard_advertise import SERVICE_TYPE
from ...helpers.dashboard_identity import (
    DASHBOARD_ID_MAX_CHARS,
    DASHBOARD_ID_PATTERN,
    get_or_create_identity,
)
from ...helpers.event_bus import Event
from ...helpers.hostname import normalize_hostname
from ...helpers.json import dumps as json_dumps
from ...helpers.json import loads as json_loads
from ...helpers.peer_link_frames import frame_schema, is_valid_frame
from ...helpers.peer_link_identity import get_or_create_peer_link_identity
from ...helpers.peer_link_resolver import PeerLinkDNSResolver, make_peer_link_resolver
from ...helpers.remote_build_cleanup import sweep_remote_builds
from ...helpers.remote_build_layout import parse_from_configuration
from ...helpers.storage import ShutdownCallback, Store
from ...models import (
    MAX_CLEANUP_TTL_SECONDS,
    MIN_CLEANUP_TTL_SECONDS,
    PAIRING_VERSION_MAX_LEN,
    TERMINAL_JOB_EVENTS,
    ErrorCode,
    EventType,
    IdentityView,
    IntentResponse,
    OffloaderAlertSnapshotEntry,
    OffloaderJobStateChangedData,
    OffloaderPairAlertDismissedData,
    OffloaderPairEndpointReboundData,
    OffloaderPairingEnabledChangedData,
    OffloaderPairPeerRevokedData,
    OffloaderPairPinMismatchData,
    OffloaderPairStatusChangedData,
    OffloaderPeerLinkClosedData,
    OffloaderPeerLinkOpenedData,
    OffloaderPeerRevokedAlert,
    OffloaderPinMismatchAlert,
    OffloaderQueueStatusChangedData,
    OffloaderRemoteBuildSettings,
    OffloaderRemoteBuildSettingsView,
    OffloaderRemoteBuildsToggledData,
    OffloaderRemoteJobSnapshotEntry,
    PairingSummary,
    PairingWindowState,
    PeerQueueStatusSnapshotEntry,
    PeerStatus,
    PeerSummary,
    QueueStatusFrameData,
    ReceiverPeerLinkSessionClosedData,
    ReceiverPeerLinkSessionOpenedData,
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
from ..config import (
    load_remote_build_settings,
    remote_build_settings_transaction,
)
from .artifacts_download import ArtifactsDownloadSender
from .artifacts_tarball import UnpackArtifactsError, unpack_artifacts_response
from .job_fanout import JobFanout
from .peer_link import PeerLinkSession, TerminateReason
from .peer_link_client import (
    DownloadArtifactsError,
    PairStatusResult,
    PeerLinkClient,
    PeerLinkClientError,
    PeerLinkNoSessionError,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
)
from .peer_link_client import (
    await_pair_status as peer_link_await_pair_status,
)
from .peer_link_client import (
    preview_pair as peer_link_preview_pair,
)
from .peer_link_client import (
    request_pair as peer_link_request_pair,
)
from .submit_job import SubmitJobReceiver

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.dashboard_identity import DashboardIdentity
    from ...helpers.peer_link_identity import PeerLinkIdentity

_LOGGER = logging.getLogger(__name__)


def _load_offloader_identities(
    config_dir: Path,
) -> tuple[PeerLinkIdentity, DashboardIdentity]:
    """Load both offloader-side identities in one executor hop.

    The peer-link X25519 keypair drives the Noise XX handshake;
    the dashboard identity carries the stable ``dashboard_id`` we
    send in msg3 so the receiver's ``StoredPeer`` row keys on it.
    The two are both lazy-create on first read, both protected by
    per-process locks in their respective helpers, and both involve
    disk I/O (each is one file read + occasional first-call
    generation). Bundling into a single sync helper means one
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


@_dataclass(frozen=True)
class _PeerLinkClientHandle:
    """Bundle a :class:`PeerLinkClient` with its run task.

    The client exposes the per-session API
    (:meth:`PeerLinkClient.submit_job`,
    :attr:`PeerLinkClient.is_session_open`); the task carries
    the cancellation handle the controller's lifecycle wiring
    needs (cancel on unpair, drain in :meth:`stop`). Held in
    :attr:`RemoteBuildController._peer_link_clients` so a single
    lookup yields both, instead of two parallel dicts that
    could drift.
    """

    client: PeerLinkClient
    task: asyncio.Task[None]


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


def _endpoints_equal(host_a: str, port_a: int, host_b: str, port_b: int) -> bool:
    """Case- and trailing-dot-insensitive equality on ``(host, port)`` pairs.

    Two paths in this controller compare an mDNS-resolved
    endpoint against another stored endpoint (the dashboard's
    own advertise via :meth:`_is_self_endpoint`, and a stored
    pairing's coordinates via :meth:`_maybe_schedule_rebind_probe`).
    Both want hostname comparison through
    :func:`normalize_hostname` so trailing-dot / case
    differences between the SRV target form and the stored form
    don't show up as spurious mismatches; this helper centralises
    the shape so the two call sites stay in lockstep on the
    normalisation rules.
    """
    return port_a == port_b and normalize_hostname(host_a) == normalize_hostname(host_b)


def _decode_txt_port(raw: bytes | None) -> int:
    """Decode a TCP port from a TXT value; fall back to 0 on absent / malformed / out-of-range.

    Defensive: a corrupted / non-numeric / out-of-range TXT
    entry shouldn't raise into the browser callback (which
    would abort the resolve task and silently lose the peer),
    and shouldn't trigger spurious rebind probes for a port
    we couldn't dial anyway. Negative integers and values
    outside the IANA ``1..65535`` range collapse to the same
    "not advertised" 0 sentinel TXT-absent rows already
    produce, so downstream sites that gate on a real port
    cover absence, malformation, and spoof attempts in one
    check.
    """
    decoded = _decode_txt_value(raw)
    if not decoded:
        return 0
    try:
        port = int(decoded)
    except ValueError:
        return 0
    if not 1 <= port <= 65535:
        return 0
    return port


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
    # ``pin_sha256`` and ``remote_build_port`` are emitted by the
    # advertiser only when the peer-link listener is bound (see
    # :class:`DashboardAdvertiser`). The offloader-side mDNS
    # auto-rebind path reads both: pin to match a broadcast
    # against a stored pairing, port to dial the
    # peer-link Noise WS on the new endpoint. ``""`` / ``0`` for
    # receivers that haven't published them; the rebind path
    # silently skips those rows.
    pin_sha256 = _decode_txt_value(properties.get(b"pin_sha256"))
    remote_build_port = _decode_txt_port(properties.get(b"remote_build_port"))
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
        pin_sha256=pin_sha256,
        remote_build_port=remote_build_port,
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


class _RebindProbeOutcome(StrEnum):
    """Typed outcome of :meth:`RemoteBuildController._probe_pairing_endpoint`.

    The probe is shared between mDNS-driven auto-rebind and
    user-driven manual edit; each caller maps the outcome onto
    its own surface (silent log + cooldown for auto, typed
    :class:`CommandError` for the WS-driven user path). The
    enum factors out the four distinct probe failure modes so
    the surface mapping lives at the call site instead of in a
    per-caller bespoke probe body.
    """

    OK = "ok"
    UNREACHABLE = "unreachable"
    PIN_MISMATCH = "pin_mismatch"
    PAIRING_REPLACED = "pairing_replaced"
    STATUS_CHANGED = "status_changed"


@_dataclass(frozen=True, slots=True)
class _RebindProbeResult:
    """Result of :meth:`RemoteBuildController._probe_pairing_endpoint`.

    *observed_pin* is populated only on
    :attr:`_RebindProbeOutcome.PIN_MISMATCH` (so the caller's
    error surface can name which identity answered at the
    candidate endpoint); *transport_error* is populated only
    on :attr:`_RebindProbeOutcome.UNREACHABLE` (the
    :class:`PeerLinkClientError` instance, kept as the
    exception itself so the auto-rebind path's debug log can
    pass it as ``exc_info=`` to preserve the traceback while
    the user-driven path can ``str()`` it for the
    :class:`CommandError` message).
    """

    outcome: _RebindProbeOutcome
    observed_pin: str = ""
    transport_error: PeerLinkClientError | None = None


# Dispatch table mapping a non-OK probe outcome to the typed
# :class:`CommandError` shape :meth:`edit_pairing_endpoint`
# raises for it. Each entry is ``(error_code, message_template)``;
# the template uses ``str.format`` with the keyword args
# ``host`` / ``port`` / ``pin`` / ``observed`` / ``error`` (all
# pre-formatted at call time so the templates stay declarative).
# Keeps the four probe-failure raise sites in
# :meth:`edit_pairing_endpoint` collapsed to one ``raise`` instead
# of four near-identical ``if … raise`` blocks.
_EDIT_PAIRING_PROBE_ERRORS: dict[_RebindProbeOutcome, tuple[ErrorCode, str]] = {
    _RebindProbeOutcome.UNREACHABLE: (
        ErrorCode.UNAVAILABLE,
        "edit_pairing_endpoint: {host}:{port} unreachable: {error}",
    ),
    _RebindProbeOutcome.PIN_MISMATCH: (
        # Different identity at the new coords. Leaves the
        # stored pairing untouched — the user's existing trust
        # is keyed on the original pin; substituting a fresh
        # pubkey under that trust is the case 8a's re-auth
        # wizard exists specifically to gate. The message
        # carries both observed and stored pin so the dialog
        # can render the "different identity at this endpoint"
        # copy and route the user to re-pair.
        ErrorCode.PRECONDITION_FAILED,
        "edit_pairing_endpoint: {host}:{port} answers with pin {observed!r}, not stored {pin!r}",
    ),
    _RebindProbeOutcome.PAIRING_REPLACED: (
        ErrorCode.NOT_FOUND,
        "edit_pairing_endpoint: pairing for pin_sha256={pin!r} changed during probe; please retry",
    ),
    _RebindProbeOutcome.STATUS_CHANGED: (
        ErrorCode.PRECONDITION_FAILED,
        "edit_pairing_endpoint: pairing status changed during probe",
    ),
}


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
    than registering twice). The actual connection runs when
    pairing attempts it (and discovers DNS / TLS validity); we
    deliberately don't pre-flight an "is this resolvable now?"
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


def _peer_summary(peer: StoredPeer, *, status: PeerStatus, connected: bool) -> PeerSummary:
    """Project a :class:`StoredPeer` to wire :class:`PeerSummary`.

    Drops the raw ``static_x25519_pub`` bytes; ``pin_sha256`` is
    the wire-friendly form UIs render for OOB-verification, and
    the pubkey is only needed server-side to look up the peer
    against an incoming Noise handshake. ``status`` is supplied
    by the caller because :class:`StoredPeer` itself doesn't
    carry one — pending peers live in the controller's in-memory
    dict and persisted peers are implicitly approved.

    ``connected`` is the snapshot-time read the caller passes
    in. The intended source is
    ``dashboard_id in controller._peer_link_sessions`` (the
    RAM-canonical receiver-side session registry the peer-link
    handshake populates); the helper is module-level rather
    than a controller method, so the caller dereferences the
    registry and threads the bool through. PENDING callers
    always pass ``False``; the structural invariant is
    enforced by the
    :meth:`RemoteBuildController.lookup_peer_for_session`
    gate, but the parameter is explicit so a future code path
    that legitimately tracks connection state on a non-APPROVED
    row doesn't inherit the hardcoded default silently.
    """
    return PeerSummary(
        dashboard_id=peer.dashboard_id,
        pin_sha256=peer.pin_sha256,
        label=peer.label,
        paired_at=peer.paired_at,
        status=status,
        peer_ip=peer.peer_ip,
        connected=connected,
    )


def _pairing_summary(
    pairing: StoredPairing,
    *,
    connected: bool,
    connecting: bool = False,
    last_connect_error: str = "",
) -> PairingSummary:
    """Project a :class:`StoredPairing` to wire :class:`PairingSummary`.

    Mirror of :func:`_peer_summary` for the offloader side. Drops
    the raw ``static_x25519_pub`` bytes. ``status`` reads off the
    row — the unified in-RAM ``_pairings`` dict carries both
    PENDING and APPROVED rows, with the disk filter stripping
    PENDING at serialise time.

    ``connected``, ``connecting``, and ``last_connect_error``
    are the snapshot-time reads the caller passes in. Intended
    sources:

    * ``connected``: ``pairing.pin_sha256 in
      controller._open_peer_links``, the RAM-canonical set the
      controller maintains from :class:`PeerLinkClient`-fired
      :attr:`EventType.OFFLOADER_PEER_LINK_OPENED` /
      :attr:`EventType.OFFLOADER_PEER_LINK_CLOSED` events.
    * ``connecting`` / ``last_connect_error``: the matching
      :class:`PeerLinkClient`'s :attr:`~PeerLinkClient.is_connecting` /
      :attr:`~PeerLinkClient.last_connect_error` properties,
      looked up via ``controller._peer_link_clients[pin_sha256]``.

    The helper is module-level so the registries aren't
    reachable from here directly; the caller dereferences and
    threads the values through. PENDING callers pass
    ``connected=False`` and let ``connecting`` /
    ``last_connect_error`` take their defaults — the offloader
    doesn't spawn a peer-link client until the receiver flips
    the row to APPROVED.
    """
    return PairingSummary(
        receiver_hostname=pairing.receiver_hostname,
        receiver_port=pairing.receiver_port,
        pin_sha256=pairing.pin_sha256,
        label=pairing.label,
        paired_at=pairing.paired_at,
        status=pairing.status,
        connected=connected,
        connecting=connecting,
        last_connect_error=last_connect_error,
        esphome_version=pairing.esphome_version,
        enabled=pairing.enabled,
    )


_PIN_SHA256_LEN = 64  # 32-byte SHA-256 → 64 lowercase-hex chars
_PAIR_LABEL_MAX_CHARS = 128  # mirrors :data:`_PEER_LABEL_MAX_CHARS` on the receiver


# Mapping from receiver-reported reject reasons (carried on
# ``artifacts_end{accepted: false}``) to the
# :class:`ErrorCode` the WS layer surfaces. Receiver-side
# constants live in :mod:`controllers.remote_build.artifacts_download`;
# the wire string is the canonical seam, not a shared
# enum, because it crosses the trust boundary as user-
# controlled JSON. Unknown reasons fall through to
# ``UNAVAILABLE`` (better safe than asserting on a
# stranger's wire format).
_DOWNLOAD_ARTIFACTS_REASON_TO_ERROR_CODE: dict[str, ErrorCode] = {
    "unknown_job": ErrorCode.NOT_FOUND,
    "build_dir_missing": ErrorCode.NOT_FOUND,
    "job_not_completed": ErrorCode.PRECONDITION_FAILED,
    "duplicate_download": ErrorCode.PRECONDITION_FAILED,
    "pack_failed": ErrorCode.UNAVAILABLE,
}


def _download_artifacts_error_to_command_error(exc: DownloadArtifactsError) -> CommandError:
    """Map the receiver's structured reject reason to a typed :class:`CommandError`.

    Used by the ``remote_build/download_artifacts`` WS command
    after :meth:`PeerLinkClient.download_artifacts` raises. The
    error's ``reason`` attribute carries the receiver's
    ``artifacts_end{accepted: false, reason}`` value verbatim;
    this helper translates it into the matching
    :class:`ErrorCode` and forwards the human-readable message.
    Unknown reasons map to ``UNAVAILABLE`` — same shape as a
    transient transport failure, since "the receiver sent a
    reason we don't recognise" is most likely a version-skew
    issue we want to surface as retry-eligible rather than a
    permanent precondition failure.
    """
    code = _DOWNLOAD_ARTIFACTS_REASON_TO_ERROR_CODE.get(exc.reason, ErrorCode.UNAVAILABLE)
    return CommandError(code, str(exc))


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


# Allowed values of ``submit_job``'s ``target`` arg. Wire-side
# the receiver enforces the same set
# (:data:`controllers.remote_build.submit_job._TARGET_TO_JOB_TYPE`);
# rejecting unknown targets here means a typo lands as a clean
# ``INVALID_ARGS`` for the frontend to render inline rather than a
# ``submit_job_ack{accepted: false, reason: "invalid_header"}``
# only after the bundle's been built and shipped.
_SUBMIT_JOB_VALID_TARGETS: frozenset[str] = frozenset({"compile", "upload"})


# Terminal ``status`` values on
# :class:`OffloaderJobStateChangedData` — drives the
# offloader-side remote-job cache's drop-on-terminal logic so
# the snapshot only carries actively-running rows. Same literal
# set the wire-frame ``Literal`` enumerates; pinned as a
# ``frozenset`` for O(1) membership.
_OFFLOADER_REMOTE_JOB_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled"}
)


# Required fields on inbound ``cancel_job`` peer-link frames.
# The frame is offloader → receiver direction; the receiver's
# :meth:`RemoteBuildController.handle_cancel_job` validates
# this shape before reaching into the :class:`JobFanout`
# correlation cache. Same defensive idiom the ``submit_job``
# dispatchers use.
_CANCEL_JOB_SCHEMA = frame_schema({"job_id": str})


def _validate_submit_job_target(raw: object) -> Literal["compile", "upload"]:
    """Validate the WS *target* arg for ``remote_build/submit_job``."""
    if not isinstance(raw, str) or raw not in _SUBMIT_JOB_VALID_TARGETS:
        msg = f"target must be one of {sorted(_SUBMIT_JOB_VALID_TARGETS)}; got {raw!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return cast(Literal["compile", "upload"], raw)


def _validate_bool(raw: object, *, command: str, field: str) -> bool:
    """Strict-bool validation for a WS-command argument.

    Rejects non-``bool`` values rather than coercing — a string
    ``"false"`` is truthy under ``bool()`` and would persist the
    opposite of the operator's intent on a security-relevant
    switch. The diagnostic names *command* and *field* so the
    frontend can pin the inline error to the right input.
    """
    if not isinstance(raw, bool):
        msg = f"{command}: {field!r} must be a boolean"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return raw


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

# 6c cleanup-sweep cadence. The sweep itself is cheap (one
# stat per subtree + an rmtree for the cold ones); the cadence
# just determines how long an expired subtree lingers before
# reclamation. Hourly keeps the worst-case lag bounded without
# burning CPU on a tight loop; the TTL itself is the
# operator-tunable knob (default 24h, see
# :data:`DEFAULT_CLEANUP_TTL_SECONDS`).
_CLEANUP_SWEEP_INTERVAL_SECONDS = 60 * 60

# Sliding window enforced per-pin between mDNS rebind probes.
# Doubles as the in-flight guard (set when scheduling, cleared
# only on probe success) and the
# retry-throttle (failure leaves the entry in place until the
# window elapses, so a permanently-unreachable host doesn't
# trigger one probe per mDNS Updated burst). 30 s is plenty
# longer than the longest plausible probe round-trip
# (Noise WS ``_DEFAULT_TIMEOUT_SECONDS`` ~ 10 s) so an in-flight
# probe never expires its own slot.
_REBIND_PROBE_COOLDOWN_SECONDS = 30.0

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
        # Shared ``aiohttp`` resolver wired to the dashboard's
        # :class:`AsyncEsphomeZeroconf` so outbound peer-link
        # connects resolve ``*.local`` receiver hostnames through
        # mDNS rather than the host OS's ``getaddrinfo``. Built
        # in :meth:`start` once the device-state monitor's
        # zeroconf is available; cleared in :meth:`stop`. Stays
        # ``None`` when the shared zeroconf isn't up (HA-addon
        # mode without an explicit ``ports:`` override, or any
        # path where zeroconf failed to start) — call sites fall
        # back to ``aiohttp``'s default OS resolver, which
        # preserves the pre-mDNS-resolver behaviour for the
        # subset of deployments that already had working mDNS in
        # the OS.
        self._peer_link_resolver: PeerLinkDNSResolver | None = None
        self._peers: dict[str, RemoteBuildPeer] = {}
        # Strong refs for fire-and-forget resolve tasks so the
        # garbage collector can't reap them mid-await.
        self._tasks: set[asyncio.Task[None]] = set()
        # mDNS auto-rebind probe slot per pin, mapping
        # ``pin_sha256`` to the monotonic timestamp at
        # which another probe is allowed. Doubles as the
        # in-flight guard (set on schedule) and the
        # retry-throttle (failure leaves it in place until the
        # cooldown elapses), so a probe storm from mDNS Updated
        # bursts and a retry hammer from a permanently-unreachable
        # host both collapse to one probe per
        # :data:`_REBIND_PROBE_COOLDOWN_SECONDS`. Successful
        # probes clear the entry — the row's stored coords now
        # match the broadcast and future broadcasts skip on the
        # equality check before they reach this map.
        self._rebind_probe_until: dict[str, float] = {}
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
        # Keyed on ``pin_sha256`` to match the unified
        # ``_pairings`` dict (offloader state is keyed on pin
        # rather than ``(host, port)`` so a receiver rename
        # is a one-line value mutation rather than a multi-
        # dict atomic remap).
        self._pair_status_listeners: dict[str, asyncio.Task[None]] = {}
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
        # ``intent="peer_link"`` Noise handshake and cleared on
        # session exit (peer close / heartbeat
        # timeout / shutdown). One entry per dashboard_id —
        # a duplicate connect kicks the older session via
        # ``TerminateReason.SUPERSEDED`` so a restarted
        # offloader takes over its previous slot rather than
        # doubling. Drained in :meth:`stop`.
        self._peer_link_sessions: dict[str, PeerLinkSession] = {}
        # Receiver-side ``submit_job`` flow handler.
        # Holds per-session in-flight bundle reception state and
        # drives the receiver's accept path
        # (assemble → write tarball → extract → queue
        # ``FirmwareJob`` with ``remote_peer`` → ack).
        # Constructed in :meth:`start` once
        # :attr:`DeviceBuilder.firmware` is available; the
        # :attr:`submit_job_receiver` property raises if accessed
        # before that — every wire-side caller is gated behind
        # the peer-link listener bind, which itself only happens
        # after ``start``, so the not-yet-installed window is
        # never reachable on a healthy code path.
        self._submit_job_receiver: SubmitJobReceiver | None = None
        # Receiver-side ``download_artifacts`` flow handler.
        # Same lifecycle as :attr:`_submit_job_receiver`:
        # constructed in :meth:`start` once the firmware
        # controller is available, accessed by the peer-link
        # receive loop's ``DOWNLOAD_ARTIFACTS`` dispatch. Nullable
        # because the controller is constructed before
        # :meth:`start`; the wire dispatch into
        # :meth:`get_artifacts_download_sender` raises if the
        # not-yet-installed window were ever reached (it isn't —
        # the peer-link listener doesn't bind until after
        # ``start``).
        self._artifacts_download_sender: ArtifactsDownloadSender | None = None
        # Receiver-side fan-out from firmware ``JOB_*`` events to
        # ``job_state_changed`` / ``job_output`` peer-link frames.
        # Subscribes in :meth:`start`, detaches in :meth:`stop`.
        # Filters firmware events by ``FirmwareJob.remote_peer``
        # so only remote-peer jobs (queued by the submit path)
        # fan out — local operator-driven compiles never reach a
        # peer-link session.
        self._job_fanout: JobFanout | None = None
        # Offloader-side long-lived peer-link clients, one per
        # APPROVED ``StoredPairing``, keyed on the receiver's
        # ``pin_sha256``. Spawned by
        # :meth:`_spawn_peer_link_client` from :meth:`start`'s
        # cold-start path and from
        # :meth:`_apply_pair_status_result` flipping a row to
        # APPROVED. Cancelled by :meth:`_cancel_peer_link_client`
        # on ``unpair``; drained in :meth:`stop`. The task runs
        # the connect-handshake-park-reconnect loop in
        # :meth:`PeerLinkClient.run`. The client object is
        # retained alongside its task so the
        # ``remote_build/submit_job`` WS command can reach
        # :meth:`PeerLinkClient.submit_job` to drive a
        # bundle through the live session — the task itself
        # exposes only ``cancel`` / ``done``, not the per-flow
        # send API.
        self._peer_link_clients: dict[str, _PeerLinkClientHandle] = {}
        # RAM-only set of ``pin_sha256`` strings whose
        # offloader-side peer-link sessions are currently open.
        # Mutated by listeners on ``OFFLOADER_PEER_LINK_OPENED``
        # (add) / ``OFFLOADER_PEER_LINK_CLOSED`` (discard) the
        # :class:`PeerLinkClient` already fires from
        # :meth:`_fire_opened` / :meth:`_fire_closed`. Read at
        # :meth:`pairings_snapshot` time to populate
        # :attr:`PairingSummary.connected` so the offloader-side
        # Settings UI's "Paired build servers" list can render a
        # connected/disconnected indicator. Cleared on
        # :meth:`unpair` for the matching pin (the row's gone;
        # any stale "true" carried over a removal would land a
        # phantom indicator on a re-pair before the handshake
        # actually completes), and on :meth:`stop` along with
        # the rest of controller-scoped state.
        self._open_peer_links: set[str] = set()
        # Identities cached once at :meth:`start` so each
        # peer-link client can pick them up without an executor
        # hop on every spawn. ``_offloader_dashboard_id`` is the
        # offloader's stable ``dashboard_id`` sent in every
        # peer_link msg3; ``_offloader_peer_link_priv`` is the
        # X25519 keypair used for the Noise XX handshake. Both
        # are loaded by the existing ``_load_offloader_identities``
        # helper.
        self._offloader_dashboard_id: str | None = None
        self._offloader_peer_link_priv: bytes | None = None
        # Single offloader-side ``StoredPairing`` map: contains both
        # PENDING and APPROVED rows, keyed on
        # :attr:`StoredPairing.pin_sha256`. The pin is the
        # stable cryptographic identity (hash of the receiver's
        # static X25519 pubkey, OOB-confirmed during preview);
        # ``(receiver_hostname, receiver_port)`` are routing
        # hints stored as fields on the value rather than the
        # primary key, so a receiver rename is a one-line
        # value mutation rather than a multi-dict atomic
        # remap. Source of truth at runtime; the disk filter at
        # serialise time strips PENDING rows so the on-disk
        # shape stays APPROVED-only. Loaded once at
        # :meth:`start` and mutated in-place on every
        # ``request_pair`` / ``unpair`` /
        # ``_apply_pair_status_result`` — saves debounce
        # through :attr:`_pairings_store`.
        self._pairings: dict[str, StoredPairing] = {}
        # 7b master toggle. Default ``True`` matches the
        # historical implicit behaviour (no setting ⇒ the
        # scheduler treats remote builds as eligible). Loaded
        # from the per-file Store at :meth:`start`; persisted
        # via the same Store on every mutation through the
        # offloader-settings WS commands. Read by
        # :meth:`build_scheduler_snapshot` on every install.
        self._remote_builds_enabled: bool = True
        # RAM-only offloader-side pair alerts. Keyed on
        # ``pin_sha256`` to match ``_pairings``. Populated by
        # ``_apply_pair_status_result`` when a
        # pair-status round-trip detects a pin drift
        # (pin_mismatch) or a receiver-side rejection
        # (peer_revoked); cleared only by the two resolution
        # paths that fix the underlying broken state:
        # ``request_pair`` succeeding for the same pin
        # (auto-resolve on re-pair) and ``unpair`` (user
        # removed the row outright). There is no operator-
        # driven dismiss surface — clicking "OK got it"
        # without acting would just hide a broken pairing the
        # next peer-link session would still fail against.
        # Never persisted: the alert describes a transient
        # detection, and a process restart with the row still
        # gone leaves nothing for the listener to re-detect
        # against; live peer-link sessions re-trigger the
        # underlying condition the next time the row is *used*.
        # ``subscribe_events.initial_state.offloader_alerts``
        # carries the snapshot so a tab subscribing AFTER the
        # event fired still sees the alert it would have missed
        # on the live stream.
        self._offloader_alerts: dict[str, OffloaderAlertSnapshotEntry] = {}
        # RAM-only offloader-side cache of the most recent
        # ``queue_status`` snapshot received from each paired
        # receiver, keyed on ``pin_sha256`` (mirrors
        # ``_pairings`` keying). Updated on every
        # inbound ``OFFLOADER_QUEUE_STATUS_CHANGED`` (the
        # :class:`PeerLinkClient` receive loop fires the event
        # after parsing a wire frame). Surfaced through
        # ``subscribe_events.initial_state.peer_queue_status`` so
        # a tab subscribing AFTER the most recent push still
        # sees the latest value the offloader has observed for
        # each peer. Never persisted — the next peer-link
        # session triggers a fresh push on its first queue
        # transition (or on session-open via the receiver-side
        # initial broadcast in a follow-up phase). Cleared on
        # :meth:`unpair` for the matching key so the snapshot
        # doesn't surface stale data for a pairing the user
        # removed.
        self._peer_queue_status: dict[str, PeerQueueStatusSnapshotEntry] = {}
        # Offloader-side cache of remote-driven jobs we submitted
        # that haven't reached a terminal state. Keyed on the
        # offloader-local ``job_id`` (the one we generated for
        # the ``submit_job`` header). Populated by the listener
        # on ``OFFLOADER_JOB_STATE_CHANGED`` and cleared on the
        # matching terminal transition. Surfaced via
        # ``subscribe_events.initial_state.remote_jobs`` so a
        # late-subscribing tab sees in-flight jobs without
        # waiting for the next event — same pattern
        # ``_peer_queue_status`` uses for queue depth. RAM-only;
        # terminal jobs drop here so a page reload after a
        # build completes shows no entry (the frontend keeps
        # its own history if it wants one).
        self._offloader_remote_jobs: dict[str, OffloaderRemoteJobSnapshotEntry] = {}
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
        # Lifecycle bag of bus-listener unsubscribers held for
        # the lifetime of the controller. Populated in
        # :meth:`start` via ``self._listeners.callback(...)`` and
        # closed in :meth:`stop`. Currently covers the
        # receiver-side firmware-queue lifecycle listeners
        # (``JOB_QUEUED`` / ``JOB_STARTED`` / terminal events) that
        # drive the ``queue_status`` peer-link broadcast and the
        # offloader-side ``OFFLOADER_QUEUE_STATUS_CHANGED`` /
        # ``OFFLOADER_PAIR_PIN_MISMATCH`` /
        # ``OFFLOADER_PEER_LINK_OPENED`` /
        # ``OFFLOADER_PEER_LINK_CLOSED`` listeners. New
        # controller-scoped bus subscriptions should register their
        # closer here so :meth:`stop` doesn't need a parallel
        # collection. ``contextlib.ExitStack`` is the stdlib
        # pattern for accumulating cleanup callables — each
        # ``EventBus.add_listener`` return is a sync ``Callable[[],
        # None]`` and ``ExitStack.callback`` is what stdlib provides
        # for that exact shape.
        self._listeners = ExitStack()

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
        # Stand up the receiver-side ``submit_job`` handler
        # before the peer-link listener can possibly fire its
        # first SUBMIT_JOB dispatch. The handler has no work to
        # do on cold start beyond holding its empty in-flight
        # dict; the wire dispatch in
        # :func:`controllers.remote_build_peer_link._receive_loop`
        # reaches it via the ``submit_job_receiver`` property,
        # which raises if accessed before this point.
        if self._db.firmware is not None:
            self._submit_job_receiver = SubmitJobReceiver(
                config_dir=self._db.settings.config_dir,
                firmware_controller=self._db.firmware,
            )
            # Stand up the receiver-side ``download_artifacts``
            # handler alongside the submit-job receiver. Same
            # firmware-controller dependency; lifecycle bound to
            # ``start`` / ``stop``.
            self._artifacts_download_sender = ArtifactsDownloadSender(
                firmware_controller=self._db.firmware,
            )
            # Subscribe to firmware ``JOB_*`` events so
            # remote-peer jobs (those carrying
            # ``FirmwareJob.remote_peer``) fan out
            # ``job_state_changed`` / ``job_output`` frames over
            # the submitting peer-link session. Lifecycle is
            # controller-bound: detached in :meth:`stop`.
            self._job_fanout = JobFanout(self)
            self._job_fanout.start()
            # Periodic TTL sweep over the remote-build
            # subtree. Lives alongside the other receiver-side
            # primitives since it reads ``firmware._jobs`` to
            # skip in-flight subtrees — gating on the same
            # ``self._db.firmware is not None`` block keeps the
            # cleanup task scoped to receivers that can
            # actually compile. ``_track_task`` reaps it through
            # the existing :meth:`stop` cancel-and-gather.
            self._track_task(
                self._run_cleanup_loop(),
                name=f"{type(self).__name__}._run_cleanup_loop",
            )
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
                self._pairings[pairing.pin_sha256] = pairing
            # 7b master toggle. Older sidecars from before
            # this field landed deserialise with the dataclass
            # default of ``True`` (mashumaro's missing-field
            # behaviour), so the load path treats absence the
            # same as "operator hasn't opted out yet".
            self._remote_builds_enabled = settings.remote_builds_enabled
        # Seed the RAM-canonical APPROVED peer dict from the
        # per-file peers store. Mirrors the offloader-side
        # ``_pairings_store`` load above; RAM is canonical from
        # this point on, every read short-circuits through
        # ``_approved_peers`` and every mutation schedules a
        # debounced write.
        if (peers_state := await self._peers_store.async_load()) is not None:
            for peer in peers_state.peers:
                self._approved_peers[peer.dashboard_id] = peer
        # Load offloader-side identities once (X25519 peer-link
        # priv + the dashboard's stable ``dashboard_id``) so each
        # peer-link client task can pick them up without a
        # per-spawn executor hop. Cold-start spawn for every
        # APPROVED pairing follows below.
        peer_link_identity, dashboard_identity = await self._load_offloader_identities_async()
        self._offloader_peer_link_priv = peer_link_identity.private_bytes
        self._offloader_dashboard_id = dashboard_identity.dashboard_id
        # Wire the shared mDNS-aware aiohttp resolver before
        # spawning peer-link clients so each client picks it up
        # at construction. The device-state monitor owns the
        # underlying :class:`AsyncZeroconf`; if it isn't up yet
        # (or failed to start in HA-addon mode), the resolver
        # stays ``None`` and outbound connects fall through to
        # ``aiohttp``'s default OS resolver — same fail-soft
        # contract as :meth:`_start_discovery` below.
        self._setup_peer_link_resolver()
        # Spawn one peer-link client task per APPROVED pairing
        # already in the dict. Each task drives the connect →
        # handshake → receive loop with auto-reconnect; the
        # task lives until ``unpair`` cancels it or
        # :meth:`stop` drains it.
        for pairing in self._pairings.values():
            if pairing.status is PeerStatus.APPROVED:
                self._spawn_peer_link_client(pairing)
        # Bus-listener registration runs unconditionally; the
        # discovery-side bring-up (mDNS browser, advertiser name
        # capture) is split into ``_start_discovery`` below and
        # runs *last* so its zeroconf-availability gate can't
        # shadow the listener registration. The listeners only
        # need ``self._db.bus`` (always present) and feed
        # ``_open_peer_links`` / ``_offloader_alerts`` /
        # ``_peer_queue_status`` / ``_offloader_remote_jobs`` —
        # none of which are zeroconf-driven, so this ordering
        # invariant is what keeps ``pairings_snapshot()``'s
        # ``connected`` projection working in test harnesses
        # (and any production path where zeroconf failed to
        # start).
        #
        # Subscribe to firmware-queue lifecycle events so every
        # transition broadcasts a fresh ``queue_status`` snapshot
        # to all paired offloaders. The transitions of interest
        # are:
        # * ``JOB_QUEUED``: a job entered the queue (queue_depth
        #   bumped; running might already be true if the runner
        #   is busy)
        # * ``JOB_STARTED``: the runner picked up the next job
        #   (queue_depth dropped, running flipped to true)
        # * Terminal events (``JOB_COMPLETED`` / ``JOB_FAILED`` /
        #   ``JOB_CANCELLED``): the runner slot is now free
        #   (running flipped to false; queue_depth may still be
        #   non-zero if more jobs were queued meanwhile)
        # ``JOB_OUTPUT`` / ``JOB_PROGRESS`` deliberately don't
        # trigger a broadcast — they're per-line streaming events
        # that fire at high rates during a build, and the
        # ``queue_status`` shape doesn't change across them.
        for event_type in (
            EventType.JOB_QUEUED,
            EventType.JOB_STARTED,
            *TERMINAL_JOB_EVENTS,
        ):
            self._listeners.callback(
                self._db.bus.add_listener(event_type, self._on_firmware_queue_transition)
            )
        # Offloader-side: subscribe to the inbound queue-status
        # bus event the :class:`PeerLinkClient` receive loop
        # fires after parsing a ``queue_status`` frame. The
        # listener mirrors the wire-shape primitives into
        # ``_peer_queue_status`` so a late ``subscribe_events``
        # snapshot reflects every paired peer's most recent
        # state. Registered into the same ``_listeners`` stack
        # as the JOB_* listeners above so :meth:`stop` walks one
        # collection.
        self._listeners.callback(
            self._db.bus.add_listener(
                EventType.OFFLOADER_QUEUE_STATUS_CHANGED,
                self._on_offloader_queue_status_changed,
            )
        )
        # Offloader-side: mirror inbound ``job_state_changed``
        # frames into ``_offloader_remote_jobs`` so a late
        # ``subscribe_events`` snapshot carries every in-flight
        # remote-driven job. The cache shape matches the wire
        # frame; terminal transitions drop the entry so the
        # snapshot only ever surfaces actively-running rows.
        self._listeners.callback(
            self._db.bus.add_listener(
                EventType.OFFLOADER_JOB_STATE_CHANGED,
                self._on_offloader_job_state_changed,
            )
        )
        # Mirror :class:`PeerLinkClient`-fired pin-mismatch
        # events into the RAM-only ``_offloader_alerts`` dict so
        # the snapshot path
        # (``subscribe_events.initial_state.offloader_alerts``)
        # picks up alerts that fired from the long-lived
        # peer-link path (peer-link handshake observed a
        # different responder pubkey than the OOB-confirmed
        # pin). Idempotent vs. the synchronous mutation in
        # :meth:`_apply_pair_status_result`'s pin-drift branch
        # — both paths land the same shape under the same key,
        # so a same-tick fire from the pair-status listener
        # ends with the listener overwriting a value the
        # caller already wrote (no behaviour change there).
        self._listeners.callback(
            self._db.bus.add_listener(
                EventType.OFFLOADER_PAIR_PIN_MISMATCH,
                self._on_offloader_pair_pin_mismatch,
            )
        )
        # Maintain ``_open_peer_links`` from the lifecycle
        # events :class:`PeerLinkClient` fires; the snapshot
        # at :meth:`pairings_snapshot` reads off the same set.
        # Two listeners (one per direction) rather than one
        # multi-event listener so each callback is straight-line
        # (add vs. discard) without an event-type branch on the
        # hot path.
        self._listeners.callback(
            self._db.bus.add_listener(
                EventType.OFFLOADER_PEER_LINK_OPENED,
                self._on_offloader_peer_link_opened,
            )
        )
        self._listeners.callback(
            self._db.bus.add_listener(
                EventType.OFFLOADER_PEER_LINK_CLOSED,
                self._on_offloader_peer_link_closed,
            )
        )
        self._start_discovery()

    async def _load_offloader_identities_async(
        self,
    ) -> tuple[PeerLinkIdentity, DashboardIdentity]:
        """Read both offloader-side identities off the executor.

        WS-command handlers and the pair-status listener
        deliberately re-read from disk on every call rather than
        using the :meth:`start`-time cache on
        :attr:`_offloader_peer_link_priv` /
        :attr:`_offloader_dashboard_id` — :meth:`rotate_identity`
        rewrites the keypair file without updating the cache,
        so caching would sign post-rotation calls with the old
        key.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, _load_offloader_identities, self._db.settings.config_dir
        )

    async def _load_settings_async(self) -> RemoteBuildSettings:
        """Read the receiver-side settings sidecar off the executor.

        Carries the ``enabled`` master toggle +
        ``cleanup_ttl_seconds`` knobs, which aren't mirrored in
        RAM (the RAM-canonical state is :attr:`_approved_peers`
        / :attr:`_pairings`).
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, load_remote_build_settings, self._db.settings.config_dir
        )

    def _setup_peer_link_resolver(self) -> None:
        """
        Build the shared mDNS-aware aiohttp resolver if zeroconf is up.

        Reads the same ``self._db.devices.zeroconf`` reference
        the discovery browser uses, so resolver-availability
        and browser-availability are bound together — either
        both run or both stay off. Stores the resolver on
        :attr:`_peer_link_resolver`; leaves it ``None`` when
        the shared zeroconf isn't available (devices controller
        not constructed, monitor failed to bind, HA-addon mode
        without zeroconf) **or** when the resolver constructor
        itself raises (e.g. the upstream
        :class:`aiohttp.resolver.AsyncResolver` ``__init__``
        raises ``RuntimeError`` when ``aiodns`` isn't installed,
        which can happen in lean env paths that drop the
        transitive dep). Fail-soft: the next ``aiohttp`` connect
        falls back to the OS resolver in either case, same
        contract as :meth:`_start_discovery`.
        """
        if self._db.devices is None:
            return
        zeroconf = self._db.devices.zeroconf
        if zeroconf is None:
            return
        try:
            self._peer_link_resolver = make_peer_link_resolver(zeroconf)
        except Exception:
            _LOGGER.exception(
                "Could not build peer-link mDNS resolver; outbound peer-link connects "
                "will fall back to the OS resolver"
            )
            self._peer_link_resolver = None

    def _start_discovery(self) -> None:
        """
        Bring up the mDNS service browser for peer discovery.

        Captures the dashboard's own service-instance name (so
        our own advertise doesn't show up in ``list_hosts``) and
        constructs the :class:`AsyncServiceBrowser` against the
        shared zeroconf. Skips silently if either the devices
        controller or its zeroconf isn't available (peer
        discovery is opt-in fail-soft); on browser-construction
        failure logs the exception and leaves :attr:`_browser`
        as ``None``.
        """
        if self._db.devices is None:
            _LOGGER.debug("remote-build discovery skipped: devices controller unavailable")
            return
        zeroconf = self._db.devices.zeroconf
        if zeroconf is None:
            _LOGGER.debug("remote-build discovery skipped: zeroconf unavailable")
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
        # Wrap browser construction so a zeroconf-side failure
        # (e.g. the underlying socket got torn down between
        # ``DeviceStateMonitor.start`` and now, or the cache is in
        # an unexpected state) doesn't abort dashboard startup.
        try:
            self._browser = AsyncServiceBrowser(
                zeroconf.zeroconf,
                [SERVICE_TYPE],
                handlers=[self._on_service_state_change],
            )
        except Exception:
            _LOGGER.exception("Could not start remote-build browser; peer discovery disabled")
            self._browser = None

    def _on_offloader_pair_pin_mismatch(self, event: Event[OffloaderPairPinMismatchData]) -> None:
        """Cache the alert in ``_offloader_alerts`` for late-subscriber snapshot.

        Keyed on ``pin_sha256`` (matches the synchronous
        mutation site in :meth:`_apply_pair_status_result`).
        The alert payload adds ``kind`` + ``fired_at`` to the
        bus event's wire fields so the snapshot row survives
        the event drop.
        """
        data = event.data
        # Build the typed alert explicitly rather than as a bare
        # dict literal: ``_offloader_alerts`` is typed
        # ``dict[..., OffloaderAlertSnapshotEntry]`` (a union of
        # ``OffloaderPinMismatchAlert`` / ``OffloaderPeerRevokedAlert``
        # discriminated by ``kind``), and a bare literal under
        # strict mypy can fall back to ``dict[str, object]``
        # rather than narrowing into the right TypedDict variant.
        alert: OffloaderPinMismatchAlert = {
            "kind": "pin_mismatch",
            "receiver_hostname": data["receiver_hostname"],
            "receiver_port": data["receiver_port"],
            "pin_sha256": data["pin_sha256"],
            "receiver_label": data["receiver_label"],
            "expected_pin": data["expected_pin"],
            "observed_pin": data["observed_pin"],
            "fired_at": time.time(),
        }
        self._offloader_alerts[data["pin_sha256"]] = alert

    def _on_offloader_peer_link_opened(self, event: Event[OffloaderPeerLinkOpenedData]) -> None:
        """Add ``pin_sha256`` to ``_open_peer_links`` and refresh the receiver version.

        The receiver advertises its
        :data:`esphome.const.__version__` on every
        ``intent_response`` payload; the offloader-side
        :class:`PeerLinkClient` lifts it onto this event so the
        controller can land the up-to-date value on the matching
        :class:`StoredPairing` without reaching into the per-pin
        client task. pick_build_path's deferred version-compat
        gate reads this field; the value refreshes on every
        reconnect so a receiver upgrade picks up on the next
        session-open without operator action.

        Empty ``esphome_version`` (older receiver predating this
        wire change, or a malformed response) leaves the stored
        value alone — clobbering with empty would lose the
        previously-captured version after a reconnect from an
        older receiver mid-rollout. Values exceeding
        :data:`PAIRING_VERSION_MAX_LEN` are also rejected: the
        :class:`StoredPairing` validator caps at the same length
        on disk-load, so persisting an oversize value through
        the in-memory mutation path would survive until the next
        sidecar load and then poison it. The wire-extract path
        on the :class:`PeerLinkClient` side already caps before
        firing this event; the listener-side guard is defense-
        in-depth for any other future fire site of the same
        event.
        """
        data = event.data
        pin_sha256 = data["pin_sha256"]
        self._open_peer_links.add(pin_sha256)
        version = data["esphome_version"]
        if not version or len(version) > PAIRING_VERSION_MAX_LEN:
            return
        pairing = self._pairings.get(pin_sha256)
        if pairing is None or pairing.esphome_version == version:
            return
        pairing.esphome_version = version
        self._schedule_pairings_save()

    def _on_offloader_peer_link_closed(self, event: Event[OffloaderPeerLinkClosedData]) -> None:
        """Discard ``pin_sha256`` from ``_open_peer_links`` on session close.

        ``discard`` rather than ``remove`` so a CLOSED event
        for a key we never saw OPENED for (cold-start race,
        unpair-mid-handshake) is a no-op rather than raising.
        """
        self._open_peer_links.discard(event.data["pin_sha256"])

    def _on_offloader_queue_status_changed(
        self, event: Event[OffloaderQueueStatusChangedData]
    ) -> None:
        """Update the offloader-side ``_peer_queue_status`` cache.

        The :class:`PeerLinkClient` receive loop validated the
        wire shape before firing the bus event, so the payload's
        primitive fields land in the snapshot dict without
        re-checking. The cached entry strips the receiver-side
        ``type`` framing — it's a snapshot of the data
        :class:`PeerQueueStatusSnapshotEntry` describes, not the
        on-the-wire :class:`QueueStatusFrameData`.
        """
        data = event.data
        self._peer_queue_status[data["pin_sha256"]] = PeerQueueStatusSnapshotEntry(
            receiver_hostname=data["receiver_hostname"],
            receiver_port=data["receiver_port"],
            pin_sha256=data["pin_sha256"],
            idle=data["idle"],
            running=data["running"],
            queue_depth=data["queue_depth"],
        )

    def peer_queue_status_snapshot(self) -> list[PeerQueueStatusSnapshotEntry]:
        """Return the offloader-side per-peer queue-status snapshot.

        Pure sync read of the in-memory cache. Used by
        :meth:`device_builder.DeviceBuilder._cmd_subscribe_events`
        to seed the offloader UI's per-peer queue display so a
        tab subscribing AFTER the most recent push still sees
        the latest value the offloader has observed for each
        paired receiver.
        """
        return list(self._peer_queue_status.values())

    def _on_offloader_job_state_changed(self, event: Event[OffloaderJobStateChangedData]) -> None:
        """Maintain the offloader-side in-flight remote-job cache.

        Upserts the entry on ``queued`` / ``running``; drops on
        terminal (``completed`` / ``failed`` / ``cancelled``)
        so the snapshot only ever carries actively-running
        rows. The :class:`PeerLinkClient` receive loop already
        validated the wire shape before firing this event.
        """
        data = event.data
        if data["status"] in _OFFLOADER_REMOTE_JOB_TERMINAL_STATUSES:
            self._offloader_remote_jobs.pop(data["job_id"], None)
            return
        self._offloader_remote_jobs[data["job_id"]] = OffloaderRemoteJobSnapshotEntry(
            receiver_hostname=data["receiver_hostname"],
            receiver_port=data["receiver_port"],
            pin_sha256=data["pin_sha256"],
            job_id=data["job_id"],
            status=data["status"],
            error_message=data["error_message"],
        )

    def offloader_remote_jobs_snapshot(self) -> list[OffloaderRemoteJobSnapshotEntry]:
        """Return the offloader-side in-flight remote-job snapshot.

        Pure sync read of the in-memory cache. Seeded into
        ``subscribe_events.initial_state.remote_jobs`` so a
        tab subscribing AFTER a job transitioned to ``running``
        still renders it without waiting for the next event.
        Terminal jobs are dropped on the matching event, so the
        snapshot never includes completed builds.
        """
        return list(self._offloader_remote_jobs.values())

    def _on_firmware_queue_transition(self, event: Event[Any]) -> None:
        """Bus listener: broadcast ``queue_status`` to paired offloaders.

        Called on every ``JOB_QUEUED`` / ``JOB_STARTED`` /
        terminal event. Builds a snapshot from the firmware
        controller's RAM state (sync read, no awaitables in the
        bus listener) and schedules a per-session broadcast as a
        background task. The broadcast itself runs async because
        it sends across N peer-link sessions and we don't want a
        slow socket on one session to block other listeners
        observing the same event.
        """
        if self._db.firmware is None:
            return
        idle, running, queue_depth = self._db.firmware.queue_status_snapshot()
        if not self._peer_link_sessions:
            return
        self._db.create_background_task(self._broadcast_queue_status(idle, running, queue_depth))

    async def _broadcast_queue_status(self, idle: bool, running: bool, queue_depth: int) -> None:
        """Send a ``queue_status`` frame to every active peer-link session.

        Snapshot the registry to a list before iterating so a
        concurrent register / unregister mid-walk doesn't mutate
        the dict under us. Each ``send_app_frame`` is gated on
        the session's ``_closing`` flag, so a ``terminate``-in-
        progress session no-ops cleanly here without raising.
        Per-session failures (``send_app_frame`` returns
        ``False``) are logged at the channel layer; we don't
        retry — a session that can't accept the latest snapshot
        will pick up the next transition's broadcast on its
        next successful frame.
        """
        sessions = list(self._peer_link_sessions.values())
        payload = QueueStatusFrameData(
            type="queue_status",
            idle=idle,
            running=running,
            queue_depth=queue_depth,
        )
        for session in sessions:
            # Per-session try/except so one flaky peer can't starve
            # broadcasts to its siblings. ``send_app_frame`` already
            # swallows the common transport / encrypt / serialise
            # failures and returns ``False``; the bare ``except``
            # here is the catch-all for an unexpected raise (e.g. a
            # mock contract drift in tests, or a future code path
            # that raises before the inner gate). Logged for
            # visibility, then we move on — the next queue
            # transition fires another snapshot.
            try:
                await session.send_app_frame(dict(payload))
            except Exception:
                _LOGGER.exception(
                    "queue_status broadcast to session %s raised; continuing with siblings",
                    session.dashboard_id,
                )

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
        # Send the receiver's current queue_status to the just-
        # connected offloader. The transition-driven broadcast
        # (``_on_firmware_queue_transition`` → ``_broadcast_queue_status``)
        # only fires when the receiver's local firmware queue
        # mutates — a cold-connected offloader that pairs before
        # the receiver builds anything would otherwise never
        # observe an idle entry, and the install scheduler's
        # ``pick_build_path`` requires an entry in
        # ``_peer_queue_status`` to consider a pairing eligible
        # (the "no signal that the receiver can accept work"
        # gate). Without this initial push the offloader's
        # ``firmware/install`` silently falls back to LOCAL on
        # every paired receiver until the receiver happens to
        # build something locally — the exact bug that surfaced
        # in production after #568 landed.
        if self._db.firmware is not None:
            try:
                idle, running, queue_depth = self._db.firmware.queue_status_snapshot()
            except Exception:
                # Best-effort: a snapshot read failure mustn't
                # poison session registration. The transition-
                # driven broadcast (``_on_firmware_queue_transition``)
                # will still catch up the offloader on the next
                # queue change. Mirrors the swallow-and-log stance
                # of :meth:`_broadcast_queue_status`.
                _LOGGER.exception(
                    "firmware.queue_status_snapshot() raised on session register; "
                    "skipping initial queue_status push to %s",
                    session.dashboard_id,
                )
            else:
                self._db.create_background_task(
                    self._send_initial_queue_status(session, idle, running, queue_depth)
                )
        # Fire AFTER the dict insert so any subscriber lookup of
        # ``_peer_link_sessions[dashboard_id]`` from inside the
        # listener observes the just-registered session.
        if self._db.bus is not None:
            self._db.bus.fire(
                EventType.RECEIVER_PEER_LINK_SESSION_OPENED,
                ReceiverPeerLinkSessionOpenedData(dashboard_id=session.dashboard_id),
            )

    async def _send_initial_queue_status(
        self,
        session: PeerLinkSession,
        idle: bool,
        running: bool,
        queue_depth: int,
    ) -> None:
        """Push a one-shot ``queue_status`` frame to a freshly-connected session.

        Mirror of :meth:`_broadcast_queue_status` but addressed
        to a single session — invoked from
        :meth:`register_peer_link_session` so the offloader gets
        an idle / running signal on cold-connect rather than
        waiting for the receiver's next firmware queue
        transition. Best-effort: a session that has already
        torn down between the registry insert and this send
        no-ops cleanly (``send_app_frame`` is gated on the
        session's ``_closing`` flag) and the offloader's next
        reconnect tries again.
        """
        payload = QueueStatusFrameData(
            type="queue_status",
            idle=idle,
            running=running,
            queue_depth=queue_depth,
        )
        try:
            await session.send_app_frame(dict(payload))
        except Exception:
            _LOGGER.exception(
                "initial queue_status to session %s raised; "
                "offloader will catch up on the next queue transition",
                session.dashboard_id,
            )

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
            # Drop any in-flight ``submit_job`` upload state so a
            # bundle reception that was mid-stream when the
            # session ended doesn't outlive the session that owns
            # it. ``_submit_job_receiver`` is set in :meth:`start`;
            # this branch only runs for sessions registered after
            # ``start`` (live wire), so the attribute is always
            # populated by the time we get here.
            if self._submit_job_receiver is not None:
                self._submit_job_receiver.discard_session(session.dashboard_id)
            # Same shape — discard any in-flight artifacts
            # download for this session so the slot doesn't
            # outlive the session it was streaming over.
            if self._artifacts_download_sender is not None:
                self._artifacts_download_sender.discard_session(session.dashboard_id)
            # Fire only when we actually dropped the slot — the
            # no-op path (a SUPERSEDED-evicted session running its
            # finally-block after the new session has taken its
            # place) would double-fire CLOSED for a single
            # logical close otherwise.
            if self._db.bus is not None:
                self._db.bus.fire(
                    EventType.RECEIVER_PEER_LINK_SESSION_CLOSED,
                    ReceiverPeerLinkSessionClosedData(dashboard_id=session.dashboard_id),
                )

    async def handle_cancel_job(self, session: PeerLinkSession, frame: dict[str, Any]) -> None:
        """Receiver-side dispatch for inbound ``cancel_job`` frames.

        Resolves the offloader-supplied ``job_id`` to the
        receiver-local :class:`FirmwareJob` via
        :meth:`JobFanout.resolve_firmware_job_id`, then routes
        the cancel through the firmware controller's existing
        :meth:`FirmwareController.cancel` primitive — same path
        as a local operator-driven cancel. No ack on the wire:
        the firmware queue fires ``JOB_CANCELLED`` once the
        cancel lands, :class:`JobFanout` fans that out as a
        ``job_state_changed{status: cancelled}`` frame, and the
        offloader's existing ``OFFLOADER_JOB_STATE_CHANGED``
        plumbing surfaces it without any new bus event type.

        Silent drops (debug-logged only) for:

        * Malformed frame shape — peer is off-contract;
          dropping matches the read-loop's behaviour for the
          other peer-controlled frames. The terminate-on-
          malformed escalation lives in
          :func:`parse_app_frame` for decrypt / JSON failures;
          a structurally-bad cancel doesn't warrant tearing
          the session down.
        * Unknown ``(remote_peer, remote_job_id)`` correlation
          — typically a race between the offloader's send and
          a receiver-side terminal transition that already
          evicted the entry. The offloader will see the
          earlier terminal ``job_state_changed`` regardless;
          no action needed.
        * :class:`CommandError` from
          :meth:`FirmwareController.cancel` (already-terminal
          job, race with a parallel cancel) — best-effort
          semantics; the offloader's UI rendered the cancel
          intent on click and the next observed state is
          authoritative.
        """
        if not is_valid_frame(_CANCEL_JOB_SCHEMA, frame):
            _LOGGER.debug(
                "peer-link cancel_job from %s: malformed frame; dropping: %r",
                session.dashboard_id,
                frame,
            )
            return
        if self._job_fanout is None or self._db.firmware is None:
            _LOGGER.debug(
                "peer-link cancel_job from %s before controller fully started; dropping",
                session.dashboard_id,
            )
            return
        remote_job_id = cast(str, frame["job_id"])
        firmware_job_id = self._job_fanout.resolve_firmware_job_id(
            session.dashboard_id, remote_job_id
        )
        if firmware_job_id is None:
            _LOGGER.debug(
                "peer-link cancel_job from %s: no firmware job for remote_job_id=%r; dropping",
                session.dashboard_id,
                remote_job_id,
            )
            return
        try:
            await self._db.firmware.cancel(job_id=firmware_job_id)
        except CommandError as exc:
            _LOGGER.debug(
                "peer-link cancel_job from %s: firmware refused cancel for job %s: %s",
                session.dashboard_id,
                firmware_job_id,
                exc.message,
            )

    def get_submit_job_receiver(self) -> SubmitJobReceiver:
        """Receiver-side ``submit_job`` flow handler.

        Accessor used by
        :func:`controllers.remote_build_peer_link._receive_loop`
        to dispatch :attr:`AppMessageType.SUBMIT_JOB` and
        :attr:`AppMessageType.SUBMIT_JOB_CHUNK` frames. Raises
        :class:`RuntimeError` if accessed before :meth:`start`
        has installed the receiver — practically unreachable
        on the live wire (the peer-link listener only binds
        after ``start``), but the explicit failure beats a
        nullable-and-skipped silent drop if a future code path
        violates the bring-up ordering.

        Plain method rather than ``@property`` because
        :func:`helpers.api.collect_api_commands` walks every
        public attribute on each controller during
        :class:`DeviceBuilder` start; a property would fire its
        getter under that walk and raise before
        :meth:`start` has run.
        """
        if self._submit_job_receiver is None:
            msg = "submit_job_receiver accessed before RemoteBuildController.start()"
            raise RuntimeError(msg)
        return self._submit_job_receiver

    def get_artifacts_download_sender(self) -> ArtifactsDownloadSender:
        """Receiver-side ``download_artifacts`` flow handler.

        Same shape as :meth:`get_submit_job_receiver`: accessed
        by :func:`controllers.remote_build.peer_link._receive_loop`
        to dispatch :attr:`AppMessageType.DOWNLOAD_ARTIFACTS`
        frames, raises :class:`RuntimeError` if accessed before
        :meth:`start` installed the sender. Same
        not-a-property rationale —
        :func:`helpers.api.collect_api_commands` walks public
        attributes at start and a property would fire too
        early.
        """
        if self._artifacts_download_sender is None:
            msg = "artifacts_download_sender accessed before RemoteBuildController.start()"
            raise RuntimeError(msg)
        return self._artifacts_download_sender

    async def stop(self) -> None:
        """Cancel the browser and drain in-flight resolve tasks."""
        if self._browser is not None:
            try:
                await self._browser.async_cancel()
            except Exception:
                _LOGGER.debug("remote-build browser cancel failed", exc_info=True)
            self._browser = None
        # Detach every bus listener registered in :meth:`start`.
        # ``ExitStack.close`` walks the registered callbacks in
        # LIFO order and calls each — every captured callback is
        # the unsubscribe handle returned by
        # ``EventBus.add_listener``, so calling it removes the
        # listener from the bus's per-event set and later fires
        # don't re-enter the controller's callbacks after it's
        # gone. Covers the receiver-side firmware-queue lifecycle
        # listeners and every offloader-side bus listener
        # registered above.
        self._listeners.close()
        # The job fan-out maintains its own listener set
        # (firmware-controller's bus, scoped to ``JOB_*`` event
        # types). Detach via the helper so the controller's
        # listener-bookkeeping doesn't fan into one shared list.
        if self._job_fanout is not None:
            self._job_fanout.stop()
            self._job_fanout = None
        await self._drain_tasks(self._tasks)
        self._tasks.clear()
        # Cancel + drain offloader-side pair-status listener tasks
        # so they don't leak past controller shutdown. Each
        # listener self-removes from ``_pair_status_listeners``
        # via its ``finally`` clause; the dict-clear at the end
        # is belt-and-braces in case a task crashed before
        # reaching its finally.
        await self._drain_tasks(self._pair_status_listeners.values())
        self._pair_status_listeners.clear()
        # Cancel + drain offloader-side peer-link client tasks.
        # Each task's run loop sends a structured
        # ``terminate{reason: client_stopped}`` to the receiver
        # in its ``CancelledError`` handler before unwinding, so
        # the receiver's session loop exits cleanly without
        # waiting for its heartbeat to time out.
        await self._drain_tasks(h.task for h in self._peer_link_clients.values())
        self._peer_link_clients.clear()
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
        self._peer_queue_status.clear()
        self._offloader_remote_jobs.clear()
        self._open_peer_links.clear()
        self._rebind_probe_until.clear()
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
        await self._close_peer_link_resolver()

    @staticmethod
    async def _drain_tasks(tasks: Iterable[asyncio.Task[Any]]) -> None:
        """Cancel and await every task in *tasks*, swallowing exceptions.

        Snapshots *tasks* to a list so the caller's post-drain
        ``clear`` doesn't pull tasks out from under the gather.
        Caller owns clearing the source collection.
        """
        tasks_list = list(tasks)
        if not tasks_list:
            return
        for task in tasks_list:
            task.cancel()
        await asyncio.gather(*tasks_list, return_exceptions=True)

    async def _close_peer_link_resolver(self) -> None:
        """
        Release the shared mDNS-aware aiohttp resolver, if any.

        Extracted from :meth:`stop` so the teardown branch lives
        next to :meth:`_setup_peer_link_resolver` (the matching
        bring-up step) instead of bloating ``stop``'s branch
        count. Safe to call multiple times: the resolver
        reference is cleared after the first call so any later
        invocation is a no-op.

        ``real_close`` releases the underlying ``aiodns``
        resources; the borrowed :class:`AsyncZeroconf` belongs
        to the device-state monitor and is closed separately on
        its stop path. The :meth:`stop` caller already drained
        every :class:`PeerLinkClient` before reaching this
        method, so no live ``aiohttp`` connector is still
        holding the resolver.
        """
        if self._peer_link_resolver is None:
            return
        try:
            await self._peer_link_resolver.real_close()
        except Exception:
            _LOGGER.debug("peer-link resolver close failed", exc_info=True)
        self._peer_link_resolver = None

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
        self._track_task(self._resolve_and_apply(zeroconf, info, name))

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

        Drops resolved entries whose ``(server, port)`` matches our
        own published advertise so this dashboard never offers
        itself as a pair candidate. The early
        ``service_instance_name`` filter in
        :meth:`_on_service_state_change` covers the common case
        before resolve, but a rename-on-conflict zeroconf bounce
        between our own register and our own browse callback can
        leave the captured instance name out of date — the
        endpoint comparison is the live cross-check that survives
        that drift. Matching on both ``server`` and ``port`` (not
        just ``server``) preserves the ability to run two
        dashboards on the same host on different ports as
        distinct peer candidates.
        """
        peer = _peer_from_service_info(name, info)
        if self._is_self_endpoint(peer.hostname, peer.port):
            return
        self._peers[name] = peer
        self._db.bus.fire(EventType.REMOTE_BUILD_HOST_ADDED, peer.to_dict())
        # mDNS auto-rebind. Same callback that fires the
        # discovered-hosts event also drives the per-pairing
        # rebind: if this broadcast carries a ``pin_sha256`` we
        # have a stored pairing for AND its ``(host, port)``
        # differs from what we have on disk, kick off a
        # probe-then-rebind background task. The probe is what
        # enforces "verify the new endpoint is actually our paired
        # receiver, not an mDNS spoof, before mutating internal
        # state."
        self._maybe_schedule_rebind_probe(peer)

    def _is_self_endpoint(self, hostname: str, port: int) -> bool:
        """Return True when *(hostname, port)* matches our published advertise.

        Reads the live ``service_target_endpoint`` off the
        :class:`DashboardAdvertiser` rather than a captured-at-start
        value so a post-start register / re-register isn't missed.
        Hostname comparison goes through
        :func:`normalize_hostname` so the resolved peer hostname
        and the advertiser's published target compare equal
        regardless of trailing-dot / case.
        """
        advertiser = self._db._dashboard_advertiser
        if advertiser is None:
            return False
        endpoint = advertiser.service_target_endpoint
        if endpoint is None:
            return False
        own_host, own_port = endpoint
        return _endpoints_equal(hostname, port, own_host, own_port)

    def _fire_host_removed(self, name: str) -> None:
        """Fire ``REMOTE_BUILD_HOST_REMOVED`` for *name*."""
        payload: RemoteBuildHostRemovedData = {"name": name}
        self._db.bus.fire(EventType.REMOTE_BUILD_HOST_REMOVED, payload)

    def _track_task(
        self, coro: Coroutine[Any, Any, None], *, name: str | None = None
    ) -> asyncio.Task[None]:
        """Schedule *coro* and hold a strong ref in :attr:`_tasks`.

        Wraps the create / track / auto-discard dance fire-and-forget
        background tasks owned by this controller need: the
        garbage collector would otherwise reap a task whose only
        reference is the local in the spawning frame. The
        :meth:`set.discard` done-callback unwires the ref the
        moment the task settles so :attr:`_tasks` doesn't grow
        unbounded.

        Distinct from :meth:`DeviceBuilder.create_background_task`
        because the controller's :meth:`stop` gathers
        :attr:`_tasks` separately for ordered subsystem
        teardown; mixing the two sets would change shutdown
        semantics.
        """
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run_cleanup_loop(self) -> None:
        """Sweep cold remote-build subtrees on a periodic tick.

        Reads the operator-configured TTL off
        :attr:`RemoteBuildSettings.cleanup_ttl_seconds`,
        collects in-flight job keys via the layout helper, and
        hands the disk walk to the executor every
        :data:`_CLEANUP_SWEEP_INTERVAL_SECONDS`. Cancel via
        :meth:`stop` fires :class:`asyncio.CancelledError`
        through the sleep so the loop settles cleanly (the
        exception is a :class:`BaseException` subclass, so the
        per-cycle ``except Exception`` below doesn't swallow
        it). Per-cycle failures (permission error inside the
        sweep, unexpected exception) are logged and the loop
        continues with the next sleep — cleanup is best-effort
        hygiene, a single bad cycle shouldn't lose the loop.

        Sleeps before the first cycle on purpose: receivers
        deploy with no accumulated subtrees (6c lands ahead of
        any production user), and the TTL is 24h, so nothing
        is eligible for reclamation for the first 24+ hours
        anyway. Firing on startup would just churn an empty
        directory.
        """
        config_dir = self._db.settings.config_dir
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(_CLEANUP_SWEEP_INTERVAL_SECONDS)
            try:
                # The spawn site in ``start`` already gates on
                # ``self._db.firmware is not None``; re-checking
                # here narrows the type for mypy and survives a
                # future controller reshape that decouples spawn
                # from start.
                firmware = self._db.firmware
                if firmware is None:
                    continue
                settings = await self._load_settings_async()
                # ``active_remote_peer_jobs`` is the public seam
                # on the firmware controller (status-and-remote_peer
                # filtered); reaching directly into ``_jobs`` here
                # would couple us to its private shape.
                in_flight_keys = frozenset(
                    rbp
                    for job in firmware.active_remote_peer_jobs()
                    if (rbp := parse_from_configuration(job.configuration)) is not None
                )
                deleted = await loop.run_in_executor(
                    None,
                    partial(
                        sweep_remote_builds,
                        config_dir,
                        ttl_seconds=settings.cleanup_ttl_seconds,
                        in_flight_keys=in_flight_keys,
                    ),
                )
                if deleted:
                    _LOGGER.info("remote-build cleanup: swept %d cold subtree(s)", deleted)
            except Exception:
                _LOGGER.exception("remote-build cleanup sweep failed")

    def _schedule_pairings_save(self) -> None:
        """Debounce-write the offloader pairings store.

        Three sites flow through here: ``request_pair`` /
        ``unpair`` / ``_apply_pair_status_result`` /
        ``_probe_and_rebind_endpoint``. The
        :data:`_PAIRINGS_SAVE_DELAY_SECONDS` window collapses
        bursts (multi-step flows that mutate the dict more than
        once before yielding) into a single disk write, and the
        ``Store``'s atomic-tempfile semantics ensure the on-disk
        shape is always either the pre-burst or the post-burst
        snapshot, never a partial.
        """
        self._pairings_store.async_delay_save(
            self._serialize_pairings, delay=_PAIRINGS_SAVE_DELAY_SECONDS
        )

    async def _probe_pairing_endpoint(
        self,
        *,
        pairing: StoredPairing,
        new_hostname: str,
        new_port: int,
    ) -> _RebindProbeResult:
        """Probe + identity-verify a candidate endpoint without mutating state.

        Caller-agnostic primitive shared by
        :meth:`_probe_and_rebind_endpoint` (mDNS-driven
        auto-rebind, 4a-o part 7) and :meth:`edit_pairing_endpoint`
        (user-driven manual rebind, 8b). Each caller maps the
        typed outcome onto its own surface — silent log + cooldown
        for the auto path, typed :class:`CommandError` for the
        WS-driven user path.

        The probe runs one ``intent="preview"`` Noise XX
        round-trip via :func:`peer_link_preview_pair`. Three
        roles in a single network call:

        * **Reachability check** — TCP connect + Noise handshake
          completing means the new endpoint is up. Any connect /
          timeout / decode error returns
          :attr:`_RebindProbeOutcome.UNREACHABLE`.
        * **Identity verification** — Noise XX binds the
          responder's static X25519 pubkey into the handshake
          transcript. A mismatch against the stored pin returns
          :attr:`_RebindProbeOutcome.PIN_MISMATCH`.
        * **Race-safe re-check** — the dict entry may have
          been replaced by a concurrent ``unpair`` / ``request_pair``
          while the probe was in flight. Identity-equality check
          on the captured ``pairing`` reference returns
          :attr:`_RebindProbeOutcome.PAIRING_REPLACED` if the
          dict no longer points at the same object, or
          :attr:`_RebindProbeOutcome.STATUS_CHANGED` if the
          row's ``status`` flipped away from APPROVED.

        Callers pre-check ``self._offloader_peer_link_priv is
        not None``; the assert here narrows for the type
        checker.
        """
        assert self._offloader_peer_link_priv is not None
        try:
            observed_pin = await peer_link_preview_pair(
                hostname=new_hostname,
                port=new_port,
                identity_priv=self._offloader_peer_link_priv,
                resolver=self._peer_link_resolver,
            )
        except PeerLinkClientError as exc:
            return _RebindProbeResult(_RebindProbeOutcome.UNREACHABLE, transport_error=exc)
        if observed_pin != pairing.pin_sha256:
            return _RebindProbeResult(_RebindProbeOutcome.PIN_MISMATCH, observed_pin=observed_pin)
        current = self._pairings.get(pairing.pin_sha256)
        if current is not pairing:
            return _RebindProbeResult(_RebindProbeOutcome.PAIRING_REPLACED)
        if current.status is not PeerStatus.APPROVED:
            return _RebindProbeResult(_RebindProbeOutcome.STATUS_CHANGED)
        return _RebindProbeResult(_RebindProbeOutcome.OK)

    def _commit_endpoint_rebind(self, pairing: StoredPairing, *, hostname: str, port: int) -> None:
        """Mutate *pairing* to (*hostname*, *port*) and run the rebind epilogue.

        Same epilogue both rebind callers (auto-rebind / user-
        driven edit) use: schedule the debounced save, cancel +
        respawn the peer-link client, fire
        :attr:`EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND`, and
        clear any per-pin auto-probe cooldown that an earlier
        failed mDNS-driven probe may have seeded — a successful
        rebind through either path means the endpoint is live
        again, so the next mDNS Updated for the same pin should
        probe immediately rather than wait the cooldown out.
        Caller is responsible for the probe + identity verify
        before calling this — no in-helper checks.
        """
        pairing.receiver_hostname = hostname
        pairing.receiver_port = port
        self._schedule_pairings_save()
        self._respawn_peer_link_at_new_endpoint(pairing)
        self._rebind_probe_until.pop(pairing.pin_sha256, None)

    def _respawn_peer_link_at_new_endpoint(self, pairing: StoredPairing) -> None:
        """Cancel + respawn the peer-link client and announce the new endpoint.

        Called after a caller has mutated *pairing*'s
        ``receiver_hostname`` / ``receiver_port`` in place to
        new coordinates. Encapsulates the three-step rebind
        epilogue:

        * cancel the old peer-link client task (parked on
          ``aiohttp.ws_connect`` against the now-dead endpoint;
          would otherwise idle there until heartbeat-timeout),
        * spawn a fresh client at the pairing's new coordinates,
        * fire :attr:`EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND`
          so subscribed frontends update display fields without
          a snapshot read.

        Used by :meth:`_probe_and_rebind_endpoint`; a future
        "manually edit a paired sender's hostname/port" surface
        would land on the same epilogue (different validate-
        and-mutate prologue, identical respawn-and-announce
        shape).
        """
        self._cancel_peer_link_client(pairing.pin_sha256)
        self._spawn_peer_link_client(pairing)
        self._fire_offloader_pair_endpoint_rebound(
            pin_sha256=pairing.pin_sha256,
            receiver_hostname=pairing.receiver_hostname,
            receiver_port=pairing.receiver_port,
        )

    # ------------------------------------------------------------------
    # mDNS auto-rebind
    # ------------------------------------------------------------------

    def _maybe_schedule_rebind_probe(self, peer: RemoteBuildPeer) -> None:
        """Spawn a probe-and-rebind task if *peer* is a known pin at a new endpoint.

        Called from :meth:`_upsert_host` on every resolved
        broadcast. Cheap early-returns dominate (most discoveries
        are unpaired peers or steady-state re-announces); only a
        rare hostname / port change for an APPROVED pairing
        spawns a probe task. The probe slot is rate-limited via
        :attr:`_rebind_probe_until` so a burst of zeroconf
        Updated callbacks or a permanently-unreachable host both
        collapse to one probe per
        :data:`_REBIND_PROBE_COOLDOWN_SECONDS`.
        """
        pin = peer.pin_sha256
        new_port = peer.remote_build_port
        if not pin or new_port == 0:
            return
        pairing = self._pairings.get(pin)
        if pairing is None or pairing.status is not PeerStatus.APPROVED:
            return
        new_hostname = normalize_hostname(peer.hostname)
        if _endpoints_equal(
            pairing.receiver_hostname, pairing.receiver_port, new_hostname, new_port
        ):
            return
        if self._offloader_peer_link_priv is None:
            return
        now = time.monotonic()
        if self._rebind_probe_until.get(pin, 0.0) > now:
            return
        self._rebind_probe_until[pin] = now + _REBIND_PROBE_COOLDOWN_SECONDS
        self._track_task(
            self._probe_and_rebind_endpoint(
                pairing=pairing, new_hostname=new_hostname, new_port=new_port
            ),
            name=f"rebind-probe-{pin[:8]}",
        )

    async def _probe_and_rebind_endpoint(
        self, *, pairing: StoredPairing, new_hostname: str, new_port: int
    ) -> None:
        """Probe the candidate endpoint; rebind the pairing iff the pin still matches.

        The probe is one ``intent="preview"`` Noise XX round-trip
        via :func:`peer_link_preview_pair` and serves three roles
        in a single network call:

        * **Reachability check** — TCP connect + Noise handshake
          completing means the new endpoint is up and answering
          peer-link traffic. Any connect / timeout / Noise error
          raises :class:`PeerLinkClientError`; we leave stored
          state alone (and let the cooldown gate the next
          attempt).
        * **Identity verification** — Noise XX binds the
          responder's static X25519 pubkey into the handshake
          transcript. A mismatch against the stored pin means a
          different keypair under the same advertised pin (mDNS
          spoof, untracked identity rotation, fresh receiver
          grabbing the old hostname); refuse to rebind.
        * **Pairing-window-independent** — ``preview`` is the
          one intent the receiver always honours, so a quiet
          receiver doesn't deadlock the rebind path.

        On match, mutate :class:`StoredPairing` in place,
        schedule the debounced save, cancel + respawn the
        peer-link client at the new coordinates, fire
        :attr:`EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND`, and
        clear the cooldown so a future move is probed
        immediately. Failure paths leave the cooldown in place.
        """
        pin = pairing.pin_sha256
        with self._clear_cooldown_on_unexpected_exit(pin):
            result = await self._probe_pairing_endpoint(
                pairing=pairing, new_hostname=new_hostname, new_port=new_port
            )
            if result.outcome is _RebindProbeOutcome.UNREACHABLE:
                # Pass the captured ``PeerLinkClientError`` as
                # ``exc_info=`` so the debug log carries the
                # full traceback for diagnosing handshake /
                # connect failures in the field — same shape
                # the inline ``except`` block had before this
                # path was factored into ``_probe_pairing_endpoint``.
                _LOGGER.debug(
                    "rebind probe %s -> %s:%d failed (unreachable / handshake error)",
                    pin,
                    new_hostname,
                    new_port,
                    exc_info=result.transport_error,
                )
                return
            if result.outcome is _RebindProbeOutcome.PIN_MISMATCH:
                _LOGGER.warning(
                    "rebind probe %s -> %s:%d observed pin %s; ignoring (spoof or rotation)",
                    pin,
                    new_hostname,
                    new_port,
                    result.observed_pin,
                )
                return
            if result.outcome is not _RebindProbeOutcome.OK:
                # PAIRING_REPLACED / STATUS_CHANGED — silent skip;
                # cooldown stays in place so a burst of mDNS
                # Updated callbacks doesn't re-fire the probe
                # against state that's already moved on.
                return
            self._commit_endpoint_rebind(pairing, hostname=new_hostname, port=new_port)
            _LOGGER.info("rebound pairing %s to %s:%d", pin, new_hostname, new_port)

    @contextmanager
    def _clear_cooldown_on_unexpected_exit(self, pin: str) -> Iterator[None]:
        """Pop *pin* from ``_rebind_probe_until`` iff the wrapped block raises.

        Graceful failure paths inside the probe (unreachable
        host, pin mismatch, mid-probe re-pair) preserve the
        cooldown entry to throttle retries. Cancellation /
        unexpected exceptions shouldn't lock the pin out of
        future legitimate rebind attempts, so on any escaped
        exception we drop the entry before the exception
        propagates.
        """
        try:
            yield
        except BaseException:
            self._rebind_probe_until.pop(pin, None)
            raise

    def _fire_offloader_pair_endpoint_rebound(
        self,
        *,
        pin_sha256: str,
        receiver_hostname: str,
        receiver_port: int,
    ) -> None:
        """Fire ``OFFLOADER_PAIR_ENDPOINT_REBOUND`` after a successful rebind."""
        payload: OffloaderPairEndpointReboundData = {
            "pin_sha256": pin_sha256,
            "receiver_hostname": receiver_hostname,
            "receiver_port": receiver_port,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_ENDPOINT_REBOUND, payload)

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
        return self._to_view(await self._load_settings_async())

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
            cleanup_ttl_seconds=settings.cleanup_ttl_seconds,
            peers=self._peer_summaries(),
        )

    def _peer_summaries(self) -> list[PeerSummary]:
        """Merge PENDING + APPROVED into a single ``PeerSummary`` list.

        ``connected`` is read off ``_peer_link_sessions`` per row.
        PENDING peers always project as ``connected=False`` (the
        peer-link dispatch refuses non-APPROVED rows; see
        :meth:`lookup_peer_for_session`); APPROVED rows look up
        their ``dashboard_id`` in the session registry to report
        live connection state. The dict membership read is
        sync, RAM-only, and constant-time per row.
        """
        sessions = self._peer_link_sessions
        return [
            _peer_summary(p, status=PeerStatus.PENDING, connected=False)
            for p in self._pending_peers.values()
        ] + [
            _peer_summary(p, status=PeerStatus.APPROVED, connected=p.dashboard_id in sessions)
            for p in self._approved_peers.values()
        ]

    def approved_peer_label(self, dashboard_id: str) -> str:
        """Return the APPROVED peer's display label, or ``""`` if not found.

        Public-by-convention accessor over the private
        ``_approved_peers`` dict so external consumers don't
        couple to the registry's internal layout. Read-only
        snapshot: returns the label as it stands right now;
        callers that need a time-of-event snapshot (e.g. the
        receiver-side ``submit_job`` flow stamping
        :attr:`FirmwareJob.remote_peer_label`) call this at the
        decisive moment rather than holding a long-lived
        reference.

        A future refactor of the peer registry (e.g. moving
        APPROVED rows into a per-file ``Store`` like
        ``_pairings``) only has to keep this accessor's
        contract; the call sites stay unchanged.
        """
        peer = self._approved_peers.get(dashboard_id)
        return peer.label if peer is not None else ""

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
    async def set_settings(
        self,
        *,
        enabled: bool,
        cleanup_ttl_seconds: int | None = None,
        **kwargs: Any,
    ) -> RemoteBuildSettingsView:
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

        Optionally accepts ``cleanup_ttl_seconds`` to update the
        6c TTL sweep's cold-subtree threshold. Validated against
        :data:`MIN_CLEANUP_TTL_SECONDS` /
        :data:`MAX_CLEANUP_TTL_SECONDS` so a fat-fingered value
        can't push the sweep to "delete everything every tick"
        or "never reclaim disk". Omitting it (or sending
        ``None``) preserves the current setting; the field is
        optional on the wire so clients that only flip the
        master switch don't have to re-supply the TTL.

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
        if cleanup_ttl_seconds is not None:
            # ``not_bool`` check first: Python's bool subclasses
            # int, so ``isinstance(True, int)`` is true. A wire
            # value of ``True`` would otherwise pass the int
            # check and bind to ``cleanup_ttl_seconds=1`` (well
            # below MIN), surfacing a confusing OUT_OF_RANGE
            # rather than the "wrong type" the operator hit.
            if isinstance(cleanup_ttl_seconds, bool) or not isinstance(cleanup_ttl_seconds, int):
                msg = "remote_build/set_settings: 'cleanup_ttl_seconds' must be an integer"
                raise CommandError(ErrorCode.INVALID_ARGS, msg)
            if not MIN_CLEANUP_TTL_SECONDS <= cleanup_ttl_seconds <= MAX_CLEANUP_TTL_SECONDS:
                msg = (
                    f"remote_build/set_settings: 'cleanup_ttl_seconds' must be between "
                    f"{MIN_CLEANUP_TTL_SECONDS} and {MAX_CLEANUP_TTL_SECONDS}"
                )
                raise CommandError(ErrorCode.INVALID_ARGS, msg)

        def _set(settings: RemoteBuildSettings) -> None:
            settings.enabled = enabled
            if cleanup_ttl_seconds is not None:
                settings.cleanup_ttl_seconds = cleanup_ttl_seconds

        view = await self._modify_settings(_set)
        await self._db.apply_remote_build_enabled()
        return view

    # ------------------------------------------------------------------
    # Offloader-side settings — the master "Remote builds enabled"
    # toggle + per-pairing enable switch. These configure the
    # ``pick_build_path`` scheduler that ``firmware/install`` routes
    # through. Mutations persist via the existing
    # ``_pairings_store`` (the master toggle lives on
    # :class:`OffloaderRemoteBuildSettings` alongside the pairings
    # list), so a single debounced save covers both kinds of edit.
    # ------------------------------------------------------------------

    def _offloader_settings_view(self) -> OffloaderRemoteBuildSettingsView:
        """Project the in-RAM offloader-side state to its wire view.

        Pure sync RAM read off :attr:`_pairings` +
        :attr:`_remote_builds_enabled`, which are canonical
        after :meth:`start` seeds them from disk.
        """
        return OffloaderRemoteBuildSettingsView(
            pairings=self.pairings_snapshot(),
            remote_builds_enabled=self._remote_builds_enabled,
        )

    @api_command("remote_build/get_offloader_settings")
    async def get_offloader_settings(self, **kwargs: Any) -> OffloaderRemoteBuildSettingsView:
        """
        Return the offloader-side settings view.

        Bundles the master ``remote_builds_enabled`` toggle
        with the projected :class:`PairingSummary` list so the
        Settings UI's first paint reads everything it needs
        from one round-trip. Subsequent live updates flow
        through ``OFFLOADER_REMOTE_BUILDS_TOGGLED`` /
        ``OFFLOADER_PAIRING_ENABLED_CHANGED`` /
        ``OFFLOADER_PAIR_STATUS_CHANGED`` events on the global
        ``subscribe_events`` stream — no polling.
        """
        return self._offloader_settings_view()

    @api_command("remote_build/set_offloader_settings")
    async def set_offloader_settings(
        self,
        *,
        remote_builds_enabled: bool,
        **kwargs: Any,
    ) -> OffloaderRemoteBuildSettingsView:
        """
        Flip the offloader-side master toggle for transparent install.

        When ``False``, :func:`pick_build_path` short-circuits
        every install to LOCAL regardless of how many idle
        receivers are paired. Peer-link sessions stay open and
        the Send-builds power-user dialog still works — only
        the implicit "Install → maybe route to a receiver"
        path is gated off. The intent is "I want to keep the
        receivers paired but stop the dashboard from
        auto-routing builds there for now."

        Strict ``bool`` validation rather than truthiness:
        the string ``"false"`` would otherwise coerce to
        ``True`` and persist the opposite of what the
        operator intended.

        Fires ``EventType.OFFLOADER_REMOTE_BUILDS_TOGGLED`` so
        other open tabs sync the switch state without
        polling, then debounce-saves through the existing
        ``_pairings_store`` (the master toggle lives on the
        same on-disk shape as the pairings list).
        """
        self._remote_builds_enabled = _validate_bool(
            remote_builds_enabled,
            command="remote_build/set_offloader_settings",
            field="remote_builds_enabled",
        )
        toggled: OffloaderRemoteBuildsToggledData = {
            "remote_builds_enabled": remote_builds_enabled,
        }
        self._db.bus.fire(EventType.OFFLOADER_REMOTE_BUILDS_TOGGLED, toggled)
        self._schedule_pairings_save()
        return self._offloader_settings_view()

    @api_command("remote_build/set_pairing_enabled")
    async def set_pairing_enabled(
        self,
        *,
        pin_sha256: str,
        enabled: bool,
        **kwargs: Any,
    ) -> PairingSummary:
        """
        Flip the per-pairing enable switch for transparent install.

        The 7b Settings UI exposes one switch per paired
        build server: a connected, healthy receiver the
        operator nevertheless doesn't want eating dashboard
        installs (flaky link, doing heavy work, in a build
        contention with another offloader). Distinct from
        ``unpair`` — the row stays in ``_pairings``, peer-link
        clients keep their sessions open, the Send-builds
        manual-dispatch path still works against this row.

        Both ``pin_sha256`` and ``enabled`` are strictly
        validated (the same shape gate
        :data:`_validate_pin_sha256` uses across this
        controller). An unknown pin raises ``NOT_FOUND``
        rather than silently no-op'ing so a stale UI doesn't
        get the wrong "switch flipped" feedback.

        Fires ``EventType.OFFLOADER_PAIRING_ENABLED_CHANGED``
        for cross-tab UI sync, then debounce-saves the
        pairings store so the choice survives restart.
        """
        clean_pin = _validate_pin_sha256(pin_sha256)
        clean_enabled = _validate_bool(
            enabled, command="remote_build/set_pairing_enabled", field="enabled"
        )
        pairing = self._pairings.get(clean_pin)
        if pairing is None:
            msg = f"remote_build/set_pairing_enabled: no pairing for pin_sha256={clean_pin!r}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        pairing.enabled = clean_enabled
        payload: OffloaderPairingEnabledChangedData = {
            "pin_sha256": clean_pin,
            "enabled": clean_enabled,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIRING_ENABLED_CHANGED, payload)
        self._schedule_pairings_save()
        return self._pairing_summary_for(pairing)

    # ------------------------------------------------------------------
    # Offloader-side pair flow — initiator commands that open Noise XX
    # WebSockets to a receiver's peer-link endpoint. The
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
        offloader call ``request_pair``.

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
                resolver=self._peer_link_resolver,
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
        peer_link_identity, dashboard_identity = await self._load_offloader_identities_async()

        try:
            result = await peer_link_request_pair(
                hostname=clean_host,
                port=clean_port,
                identity_priv=peer_link_identity.private_bytes,
                label=clean_offloader_label,
                dashboard_id=dashboard_identity.dashboard_id,
                resolver=self._peer_link_resolver,
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
        key = result.pin_sha256
        # Sweep any stale entry at the same ``(host, port)`` but
        # under a different pin (rotation, or the user moved a
        # different receiver to this hostname): drop the row,
        # cancel its listener + peer-link client, drop its
        # alert. Without this, a re-pair under a fresh pin
        # would leave the old pin's row + listener orphaned.
        # The pin-keyed lookup of the *new* row happens after,
        # so the freshly-keyed entry isn't accidentally evicted
        # if the new pin happens to equal the swept one (in
        # which case the loop's ``key != ...`` guard skips it).
        self._sweep_stale_pairings_at_endpoint(clean_host, clean_port, keep_pin_sha256=key)
        # Re-pair against an existing entry under the SAME pin
        # (the receiver hasn't rotated; user just re-confirmed
        # the same identity) means we update the row in place;
        # the *existing* listener task captured the old pairing
        # in its closure and would compare incoming pin_sha256
        # against the stale value, so cancel it explicitly here
        # before deciding whether to spawn a fresh listener.
        # The cancelled task self-removes from
        # ``_pair_status_listeners`` via its ``finally`` clause.
        self._pairings[key] = pairing
        self._cancel_pair_status_listener(key)
        # Auto-resolve any prior pin_mismatch / peer_revoked
        # alert for this receiver: the user just successfully
        # re-paired (the new row is in ``_pairings`` above), so
        # the alert is stale. Fires
        # ``OFFLOADER_PAIR_ALERT_DISMISSED`` for cross-tab sync.
        self._dismiss_offloader_alert(key, clean_host, clean_port)
        if target_status is PeerStatus.APPROVED:
            # Persisted trust anchor that survives restart. Schedule
            # the debounced disk write; the controller's ``stop()``
            # flushes any still-pending save through the registered
            # shutdown callback.
            self._schedule_pairings_save()
            # APPROVED row → spawn the long-lived peer-link
            # client. Receiver already authenticated us via the
            # pair_request; the client just opens a
            # peer_link session against the same coordinates.
            self._spawn_peer_link_client(pairing)
            # The just-spawned handle drives the response: the
            # task is alive and its first connect attempt is in
            # flight, so ``connecting`` resolves to ``True`` even
            # though ``connected`` is still ``False`` (the
            # post-handshake fire of ``OFFLOADER_PEER_LINK_OPENED``
            # is what flips ``connected`` to ``True``).
            return self._pairing_summary_for(pairing)
        # PENDING: in-memory only, bounded by the receiver-side
        # pairing window. The listener observes the eventual flip
        # (admin Accept) and promotes the row in
        # ``_apply_pair_status_result`` — which mutates the dict
        # entry's ``status`` and schedules a save.
        self._spawn_pair_status_listener(pairing)
        return self._pairing_summary_for(pairing)

    @api_command("remote_build/unpair")
    async def unpair(
        self,
        *,
        pin_sha256: str,
        **kwargs: Any,
    ) -> dict[str, bool]:
        """Drop the local :class:`StoredPairing` row keyed on *pin_sha256*.

        Idempotent: returns ``{"removed": False}`` when no row matches
        rather than raising — the frontend's "Unpair" button should
        always succeed visually even when the row was already gone
        (race with a concurrent listener-driven flip-to-removed).

        4a-o part 6 changed the WS arg from ``(hostname, port)`` to
        ``pin_sha256`` so the receiver-side identity (not the
        routing coordinates) is the lookup key — this keeps unpair
        consistent across a hostname change. The frontend already
        carries ``pin_sha256`` on every :class:`PairingSummary`,
        so the user-clicks-Unpair path threads that value
        directly.

        Receiver-side state is *not* notified. The receiver's
        :class:`StoredPeer` row stays until the receiver's admin
        clicks Remove on their Pairing requests inbox; that's the
        receiver's ownership concern. The next ``intent="peer_link"``
        from this offloader will return ``REJECTED`` because the
        offloader's local row is gone, but the receiver-side row
        won't auto-clean — a future re-auth wizard would surface
        the "stale on receiver, removed locally" case as a UI
        affordance for the receiver-side admin to clean up.

        If a pair-status listener task is in flight for this row
        (admin hadn't clicked Accept/Reject yet), it gets cancelled
        promptly so the offloader's open Noise WS to the receiver
        closes cleanly rather than waiting on a now-irrelevant flip.
        """
        key = _validate_pin_sha256(pin_sha256)

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
        self._cancel_pair_status_listener(key)
        # Cancel the long-lived peer-link client too — same
        # rationale (the client holds an open Noise WS that
        # should close promptly on user-clicks-Unpair).
        self._cancel_peer_link_client(key)
        # Single in-RAM dict carries both PENDING and APPROVED.
        previous = self._pairings.pop(key, None)
        if previous is None:
            return {"removed": False}
        # APPROVED rows live on disk; the debounced save flushes
        # the deletion. PENDING rows aren't on disk anyway, but
        # scheduling a save on every removal keeps the code path
        # uniform — the eventual write rebuilds the pairings list
        # from RAM regardless of what the dropped row's status was.
        self._schedule_pairings_save()
        # Fire the local bus event so other clients on the global
        # ``subscribe_events`` stream see the removal without
        # re-fetching the pairings snapshot. Mirrors the
        # receiver-side ``remove_peer`` firing the same shape.
        self._fire_offloader_pair_status_changed(
            previous.receiver_hostname, previous.receiver_port, key, "removed"
        )
        # Drop any pending pin_mismatch / peer_revoked alert
        # for this receiver — the user explicitly removed the
        # row, so the alert about it is moot. Fires
        # ``OFFLOADER_PAIR_ALERT_DISMISSED`` for cross-tab sync.
        self._dismiss_offloader_alert(key, previous.receiver_hostname, previous.receiver_port)
        # Drop any cached queue-status snapshot for this
        # receiver. Without this, ``subscribe_events`` would
        # keep surfacing a stale snapshot of a pairing the user
        # just removed; the offloader has no live peer-link
        # session left to refresh it from. No bus event needs
        # firing — the ``removed`` ``OFFLOADER_PAIR_STATUS_CHANGED``
        # already tells subscribers the row is gone, and the
        # frontend is expected to drop derived per-peer state
        # in step.
        self._peer_queue_status.pop(key, None)
        # Drop any in-flight remote-job snapshot entries for the
        # unpaired peer — the peer-link client is being torn
        # down, so no more lifecycle events will arrive for
        # these jobs and the snapshot must not surface them as
        # "still running" forever.
        for job_id, entry in list(self._offloader_remote_jobs.items()):
            if entry["pin_sha256"] == key:
                self._offloader_remote_jobs.pop(job_id, None)
        # Same rationale for ``_open_peer_links`` — the row is
        # gone, so any stale "true" carried over the removal
        # would land a phantom indicator on a re-pair before
        # the new peer-link client's handshake actually
        # completes. ``discard`` is no-op if the key wasn't
        # present (PENDING removal, never-connected APPROVED).
        self._open_peer_links.discard(key)
        return {"removed": True}

    @api_command("remote_build/edit_pairing_endpoint")
    async def edit_pairing_endpoint(
        self,
        *,
        pin_sha256: str,
        hostname: str,
        port: int,
        **kwargs: Any,
    ) -> PairingSummary:
        """Manually rebind *pin_sha256*'s pairing onto new (*hostname*, *port*) coords.

        User-driven analog of 4a-o part 7's mDNS auto-rebind, for
        the cases the auto-rebind can't catch: cross-subnet
        receivers (no mDNS path), mDNS disabled on the
        receiver's host, the receiver moved to a hostname the
        offloader's network can resolve but mDNS doesn't
        broadcast.

        Same trust model as the auto-rebind: a one-shot
        :func:`peer_link_preview_pair` probe verifies the new
        endpoint is reachable AND answers with the same pin
        :class:`StoredPairing` was paired against. The probe
        replaces the entire identity-verification gate — we
        deliberately don't fall through to the normal peer-link
        client retry loop on a pin mismatch, since accepting a
        new identity under the user's existing trust is
        precisely what 8a's re-auth wizard is for.

        Args:
            pin_sha256: Identity of the existing pairing — RAM
                key (not the routing coordinates, which are what
                we're updating).
            hostname: New routing host (validated as for manual
                hosts via :func:`_validate_hostname`: non-empty,
                ≤255 chars, trimmed + ``str.lower``-normalised
                so the stored value's case is consistent. The
                trailing-dot / FQDN-form folding the
                no-op-edit guard cares about happens inside
                :func:`_endpoints_equal`'s
                :func:`normalize_hostname` call at compare time
                only; the value persisted on
                :class:`StoredPairing.receiver_hostname` keeps
                the trim + lowercase shape the validator
                returned, matching what :meth:`request_pair`
                writes.
            port: New peer-link port (1-65535, non-bool).

        Returns:
            Updated :class:`PairingSummary` projection of the
            re-routed row. ``connected`` typically reads
            ``False`` at return time — the new
            :class:`PeerLinkClient` task spawned by
            :meth:`_respawn_peer_link_at_new_endpoint` is still
            running its handshake; the
            :attr:`EventType.OFFLOADER_PEER_LINK_OPENED` event
            that follows flips it to ``True`` once the session
            opens.

        Raises:
            :class:`CommandError(INVALID_ARGS)` for bad inputs.
            :class:`CommandError(NOT_FOUND)` if no pairing
                exists for *pin_sha256*, or the pairing dropped
                mid-probe.
            :class:`CommandError(PRECONDITION_FAILED)` if the
                pairing isn't APPROVED, the offloader-side
                identity hasn't loaded yet, the new endpoint
                matches the current one (no-op), or the probe
                lands at a different identity (pin mismatch:
                the user must re-pair through the regular pair
                flow if they actually want to switch identities).
            :class:`CommandError(UNAVAILABLE)` if the new
                endpoint is unreachable / the Noise XX
                handshake fails — leaves the stored pairing
                untouched so a retry against the correct coords
                lands cleanly.
        """
        pin = _validate_pin_sha256(pin_sha256)
        clean_host = _validate_hostname(hostname, context=_HostFieldContext.RECEIVER)
        clean_port = _validate_port(port, context=_HostFieldContext.RECEIVER)

        pairing = self._pairings.get(pin)
        if pairing is None:
            msg = f"edit_pairing_endpoint: no pairing for pin_sha256={pin!r}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        if pairing.status is not PeerStatus.APPROVED:
            msg = f"edit_pairing_endpoint: pairing status is {pairing.status.value!r}, not APPROVED"
            raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
        # System-readiness check before user-input semantics:
        # surface "identity not loaded yet" distinctly rather
        # than a confusing "matches current" error when a user
        # happens to hit Save with unchanged coords during
        # startup.
        if self._offloader_peer_link_priv is None:
            msg = "edit_pairing_endpoint: offloader peer-link identity not loaded yet"
            raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
        if _endpoints_equal(
            pairing.receiver_hostname, pairing.receiver_port, clean_host, clean_port
        ):
            msg = f"edit_pairing_endpoint: new endpoint matches current ({clean_host}:{clean_port})"
            raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)

        result = await self._probe_pairing_endpoint(
            pairing=pairing, new_hostname=clean_host, new_port=clean_port
        )
        if result.outcome is not _RebindProbeOutcome.OK:
            # Table-driven dispatch: every non-OK probe outcome
            # maps to a typed :class:`CommandError` via
            # :data:`_EDIT_PAIRING_PROBE_ERRORS`. Templates take
            # all five format keys; unused ones are ignored by
            # :meth:`str.format`. Keeps the rationale for each
            # failure mode at the table site instead of buried
            # in a four-branch chain.
            code, template = _EDIT_PAIRING_PROBE_ERRORS[result.outcome]
            raise CommandError(
                code,
                template.format(
                    host=clean_host,
                    port=clean_port,
                    pin=pin,
                    observed=result.observed_pin,
                    error=result.transport_error,
                ),
            )
        self._commit_endpoint_rebind(pairing, hostname=clean_host, port=clean_port)
        return self._pairing_summary_for(pairing)

    async def _validate_submit_job_config(self, configuration: object) -> tuple[str, Path]:
        """Validate the WS *configuration* arg, return ``(name, yaml_path)``.

        Validates the path-traversal boundary via
        :meth:`DashboardSettings.rel_path` and returns the
        resolved :class:`Path` so the downstream bundle build
        doesn't have to redo the executor hop. ``rel_path`` is
        blocking (``Path.resolve`` = ``os.path.abspath``
        syscall) so the call lives inside an executor.
        Mirrors
        :meth:`FirmwareController._validate_configuration_boundary`'s
        shape; lifted as a private helper so the future
        bulk-submit variant (multi-config offload) can reuse
        the same gate.
        """
        if not isinstance(configuration, str) or not configuration:
            msg = "configuration must be a non-empty string"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        loop = asyncio.get_running_loop()
        yaml_path = await loop.run_in_executor(None, self._db.settings.rel_path, configuration)
        return configuration, yaml_path

    def _lookup_open_peer_link_client(self, pin_sha256: str, *, label: str) -> PeerLinkClient:
        """Return the live :class:`PeerLinkClient` for *pin_sha256*, raising on miss.

        Two error codes the frontend branches on: ``NOT_FOUND``
        for a missing pairing (typo / row removed concurrently),
        ``PRECONDITION_FAILED`` for any of the
        not-ready-for-traffic states (PENDING, client not
        spawned, orphaned, mid-reconnect). The latter four are
        folded into one raise — the user's recovery is the
        same (wait + retry); the distinguishing detail is for
        the operator's log line, not a UI branch.

        ``label`` names the calling operation in the
        :class:`CommandError` message (``"submit_job"`` /
        ``"cancel_job"`` / future senders) so the user-facing
        text identifies which WS command failed rather than
        always saying ``"submit_job: ..."``.
        """
        pairing = self._pairings.get(pin_sha256)
        if pairing is None:
            msg = f"{label}: no pairing for pin_sha256={pin_sha256!r}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        if pairing.status is not PeerStatus.APPROVED:
            reason = f"status is {pairing.status.value!r}, not APPROVED"
        elif (handle := self._peer_link_clients.get(pin_sha256)) is None:
            reason = "client not yet spawned"
        elif handle.task.done():
            reason = "client orphaned (pin mismatch / superseded)"
        elif not handle.client.is_session_open:
            reason = "session not connected (mid-reconnect / receiver offline)"
        else:
            return handle.client
        msg = f"{label}: peer-link to {pairing.label!r} not ready ({reason})"
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)

    async def _build_submit_job_bundle(self, configuration: str, yaml_path: Path) -> bytes:
        """Build the bundle bytes for *yaml_path*.

        Wraps :func:`helpers.config_bundle.build_yaml_bundle`
        (which spawns the ``esphome bundle`` CLI). Maps the
        two structured failure modes
        (:class:`FileNotFoundError`, :class:`BundleBuildError`)
        to typed :class:`CommandError`; anything else
        propagates and lands as ``INTERNAL_ERROR`` via the WS
        dispatcher's outer ``except Exception``.

        *configuration* is the original wire-arg, used only for
        diagnostic messages; *yaml_path* is the resolved path
        :meth:`_validate_submit_job_config` already produced.
        """
        from ...helpers.config_bundle import (  # noqa: PLC0415
            BundleBuildError,
            build_yaml_bundle,
        )

        try:
            return await build_yaml_bundle(yaml_path)
        except FileNotFoundError as exc:
            raise CommandError(
                ErrorCode.NOT_FOUND, f"submit_job: YAML not found: {configuration}"
            ) from exc
        except BundleBuildError as exc:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"submit_job: bundle build failed for {configuration}: {exc.output or exc}",
            ) from exc

    @api_command("remote_build/submit_job")
    async def submit_job(
        self,
        *,
        pin_sha256: str,
        configuration: str,
        target: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Bundle *configuration* and dispatch a build to the receiver behind *pin_sha256*.

        Offloader-side counterpart of the receiver's
        :class:`SubmitJobReceiver` accept path. Validates the
        request, packs the config + every referenced file
        (includes, secrets, fonts, images, …) into a gzipped
        tarball via the ``esphome bundle`` CLI subprocess
        (:func:`helpers.config_bundle.build_yaml_bundle`), and
        streams the bytes over the existing peer-link session.
        Returns the receiver's ``submit_job_ack`` shape so the
        frontend can render success / rejection inline; live
        job lifecycle + output are pushed asynchronously
        through ``OFFLOADER_JOB_STATE_CHANGED`` /
        ``OFFLOADER_JOB_OUTPUT`` events on the
        ``subscribe_events`` stream.

        Validation gates (in order, so the cheapest user-input
        errors short-circuit before we touch disk or the wire):

        1. ``pin_sha256`` shape (lowercase 64-hex).
        2. ``target`` value (one of ``compile`` / ``upload``).
        3. ``configuration`` shape + path-traversal boundary
           (resolves to a leaf YAML under ``config_dir``).
        4. Pairing exists, status is APPROVED.
        5. Peer-link client exists and a session is currently
           live.

        After validation, build the bundle by spawning
        ``esphome bundle <yaml> -o <tmp.tar.gz>`` with a 60s
        timeout (see
        :func:`helpers.config_bundle.build_yaml_bundle`).
        Subprocess instead of in-process because the CLI is the
        stable upstream contract (the in-process
        ``read_config`` + ``ConfigBundleCreator`` would couple
        us to ``CORE.config_path`` + the validation pipeline,
        both of which shift across ESPHome releases). Generate
        a fresh ``job_id`` and hand off to
        :meth:`PeerLinkClient.submit_job`. The bundle is
        rebuilt every call: a stale cache would ship the wrong
        source after the user edits a YAML.

        Returns:
            ``{"job_id": <our id>, "accepted": <bool>,
              "reason": <str>}`` — ``reason`` only present on
              rejection (matches :class:`SubmitJobAckFrameData`'s
              ``NotRequired[str]``).

        Raises:
            :class:`CommandError(INVALID_ARGS)` for bad inputs
                (pin / target / configuration shape) or a
                ``esphome bundle`` non-zero exit (schema-
                invalid YAML, missing include, malformed
                secret — the CLI's stdout is inlined into
                the message).
            :class:`CommandError(NOT_FOUND)` if no pairing
                exists for *pin_sha256* or the YAML is missing
                from ``config_dir``.
            :class:`CommandError(PRECONDITION_FAILED)` if the
                pairing isn't APPROVED, or the peer-link
                session isn't currently live (orphaned client,
                receiver unreachable, mid-reconnect).
            :class:`CommandError(UNAVAILABLE)` if the wire-side
                send fails mid-flow or the ack times out (the
                session may have died between the open check
                and the send; the receiver may have been
                slow under load).
            :class:`CommandError(INTERNAL_ERROR)` for
                unexpected failures inside the bundle
                subprocess (e.g. ``esphome`` not on PATH).
        """
        clean_pin = _validate_pin_sha256(pin_sha256)
        clean_target = _validate_submit_job_target(target)
        clean_config, yaml_path = await self._validate_submit_job_config(configuration)
        client = self._lookup_open_peer_link_client(clean_pin, label="submit_job")
        # Build the bundle off the event loop. Any
        # ``BundleBuildError`` (CLI schema failure, missing
        # include, malformed secret) maps to INVALID_ARGS so the
        # user gets the validator's stdout verbatim; any other
        # exception lands as INTERNAL_ERROR.
        bundle_bytes = await self._build_submit_job_bundle(clean_config, yaml_path)
        job_id = uuid4().hex[:12]
        try:
            ack = await client.submit_job(
                job_id=job_id,
                configuration_filename=clean_config,
                target=clean_target,
                bundle_bytes=bundle_bytes,
            )
        except PeerLinkNoSessionError as exc:
            raise CommandError(ErrorCode.PRECONDITION_FAILED, str(exc)) from exc
        except (SubmitJobTimeoutError, SubmitJobSessionLostError) as exc:
            raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc
        result: dict[str, Any] = {
            "job_id": ack["job_id"],
            "accepted": ack["accepted"],
        }
        if "reason" in ack:
            result["reason"] = ack["reason"]
        return result

    @api_command("remote_build/download_artifacts")
    async def download_artifacts(
        self,
        *,
        pin_sha256: str,
        job_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Fetch the build's flash-artifact set for *job_id* from the paired receiver.

        Sends ``download_artifacts{job_id}`` over the open
        peer-link to *pin_sha256*, parks on the assembled-bytes
        future the receive-loop fills as
        ``artifacts_start`` / ``artifacts_chunk`` /
        ``artifacts_end`` frames land, then unpacks the
        SHA-256-verified gzipped tarball into a structured
        response the offloader's frontend can hand directly to
        its install paths (Web Serial / network OTA /
        download-to-disk).

        Validation gates (cheapest first, so user-input errors
        short-circuit before any wire work):

        1. ``pin_sha256`` shape (lowercase 64-hex).
        2. ``job_id`` shape (non-empty string).
        3. Pairing exists, status is APPROVED, peer-link
           session live.

        After validation, hand off to
        :meth:`PeerLinkClient.download_artifacts` for the
        round-trip; on success, unpack the tarball off the
        event loop (executor) and rewrite
        ``idedata.extra.flash_images[].path`` from the
        receiver's absolute build-dir paths to the bare
        basenames the offloader's install path looks up in
        the returned ``images`` list.

        Returns:
            ``{job_id, idedata, images, total_bytes}`` —
            ``idedata`` is the parsed manifest (with rewritten
            paths), ``images`` is a list of
            ``{name, offset, size, data_b64}`` entries
            (``firmware.bin`` first, then
            ``idedata.extra.flash_images`` in their declared
            order), and ``total_bytes`` is the sum of every
            image's ``size`` for the frontend's progress UI.

        Raises:
            :class:`CommandError(INVALID_ARGS)` for bad
                inputs (pin / empty job_id) or a malformed
                tarball from the receiver.
            :class:`CommandError(NOT_FOUND)` if no pairing
                exists for *pin_sha256*, or the receiver
                reported ``unknown_job`` /
                ``build_dir_missing`` (the job doesn't exist
                on the receiver, or its build dir was wiped
                by the cleanup sweep before download).
            :class:`CommandError(PRECONDITION_FAILED)` if the
                pairing isn't APPROVED, the peer-link
                session isn't live, or the receiver reported
                ``job_not_completed`` /
                ``duplicate_download`` (job still running, or
                another download is already streaming for
                this session).
            :class:`CommandError(UNAVAILABLE)` if the wire
                send fails mid-flow, the session ends mid-
                download, or the receiver reported
                ``pack_failed`` (build artifacts couldn't be
                read on the receiver — disk error, race with
                cleanup).
        """
        clean_pin = _validate_pin_sha256(pin_sha256)
        if not isinstance(job_id, str) or not job_id:
            msg = "job_id must be a non-empty string"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        client = self._lookup_open_peer_link_client(clean_pin, label="download_artifacts")
        try:
            packed = await client.download_artifacts(job_id=job_id)
        except PeerLinkNoSessionError as exc:
            raise CommandError(ErrorCode.PRECONDITION_FAILED, str(exc)) from exc
        except SubmitJobSessionLostError as exc:
            raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc
        except DownloadArtifactsError as exc:
            raise _download_artifacts_error_to_command_error(exc) from exc
        try:
            return await asyncio.get_running_loop().run_in_executor(
                None, unpack_artifacts_response, packed, job_id
            )
        except UnpackArtifactsError as exc:
            raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc

    @api_command("remote_build/cancel_job")
    async def cancel_job(
        self,
        *,
        pin_sha256: str,
        job_id: str,
        **kwargs: Any,
    ) -> dict[str, bool]:
        """Send a ``cancel_job`` frame to the receiver behind *pin_sha256*.

        Cooperative cancellation for a previously-submitted
        remote-driven job. *job_id* is the offloader-local id
        the original :meth:`submit_job` returned. The handler
        validates the pairing + session, then fires the frame
        through :meth:`PeerLinkClient.cancel_job` — fire-and-
        forget; the receiver's resulting
        ``job_state_changed{status: cancelled}`` frame is the
        confirmation, surfaced through the existing
        :attr:`EventType.OFFLOADER_JOB_STATE_CHANGED` plumbing.

        Returns ``{"sent": <bool>}`` reflecting whether the
        frame made it onto the wire. ``sent=false`` means a
        same-tick channel failure (Noise encrypt / WS send);
        the caller should treat it the same as a typed error
        — the cancel didn't reach the receiver.

        Raises:
            :class:`CommandError(INVALID_ARGS)` for bad inputs
                (pin / empty job_id).
            :class:`CommandError(NOT_FOUND)` if no pairing
                exists for *pin_sha256*.
            :class:`CommandError(PRECONDITION_FAILED)` if the
                pairing isn't APPROVED, or the peer-link
                session isn't currently live.
        """
        clean_pin = _validate_pin_sha256(pin_sha256)
        if not isinstance(job_id, str) or not job_id:
            msg = "job_id must be a non-empty string"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        client = self._lookup_open_peer_link_client(clean_pin, label="cancel_job")
        try:
            sent = await client.cancel_job(job_id=job_id)
        except PeerLinkNoSessionError as exc:
            raise CommandError(ErrorCode.PRECONDITION_FAILED, str(exc)) from exc
        return {"sent": sent}

    def get_pairing(self, pin_sha256: str) -> StoredPairing | None:
        """
        Return the :class:`StoredPairing` for *pin_sha256*, or ``None``.

        Pure synchronous read of the unified ``_pairings`` dict.
        Used by the firmware controller's install-source resolver
        after :func:`helpers.build_scheduler.pick_build_path` has
        chosen a receiver — the resolver needs the pairing's
        ``label`` to stamp on the new :class:`FirmwareJob`'s
        ``source_label`` so the install dialog's "Building on
        {receiver_label}" sub-line has a name to render.
        """
        return self._pairings.get(pin_sha256)

    def remote_builds_enabled_snapshot(self) -> bool:
        """
        Return the current value of the 7b master toggle.

        Pure synchronous RAM read. Used by
        :meth:`DeviceBuilder._cmd_subscribe_events` to seed
        the offloader Settings UI's "Remote builds enabled"
        switch on first paint; subsequent updates flow via
        ``OFFLOADER_REMOTE_BUILDS_TOGGLED`` events on the
        same stream.

        The scheduler doesn't go through this — it reads
        :attr:`_remote_builds_enabled` directly via
        :meth:`build_scheduler_snapshot`. The named helper is
        purely the subscribe-events seed point so the UI
        consumer doesn't reach into a private attribute.
        """
        return self._remote_builds_enabled

    def build_scheduler_snapshot(self) -> BuildSchedulerInputs:
        """
        Bundle the scheduler's input state into an immutable snapshot.

        Pure synchronous read of three RAM-canonical dicts plus
        a master toggle: ``_pairings`` (every paired receiver,
        PENDING + APPROVED), ``_open_peer_links`` (pin_sha256 set
        of currently-live peer-link sessions), and
        ``_peer_queue_status`` (most recent ``queue_status``
        snapshot per pin). Construction is sync + side-effect-
        free so the ``firmware/install`` WS handler can call it
        without an executor hop on the hot install path.

        The :class:`BuildSchedulerInputs` typing
        (``Mapping[str, StoredPairing]`` + ``frozenset[str]`` +
        ``Mapping[str, PeerQueueStatusSnapshotEntry]``) forces
        the caller into read-only iteration: a concurrent
        mutation on the controller's underlying *mapping*
        membership during a long-running install (a fresh
        pairing landing on a different event-loop tick)
        doesn't poison the snapshot the scheduler is walking.

        The shallow copy doesn't extend to the
        :class:`StoredPairing` rows themselves —
        ``StoredPairing`` is a mutable ``@dataclass`` and is
        edited in place elsewhere
        (e.g. ``_apply_pair_status_result`` updates
        ``esphome_version`` on an existing row;
        ``_commit_endpoint_rebind`` rewrites the hostname /
        port pair). The scheduler today is the only consumer,
        runs sync on the same event-loop tick as the install
        handler, and reads the four scalar fields
        (``status`` / ``paired_at`` / ``pin_sha256`` /
        ``esphome_version``) that aren't being mutated by any
        in-flight call. If a future consumer needs a deep
        snapshot stable across awaits, the right shape is
        ``BuildSchedulerInputs.pairings: Mapping[str,
        PairingSummary]`` (frozen-by-projection at construction
        time) — flagged here so the surface choice is a
        deliberate move, not a silent assumption.

        ``remote_builds_enabled`` reads :attr:`_remote_builds_enabled`,
        the master switch the offloader Settings UI flips
        through ``set_offloader_settings``. ``False``
        gates every install to LOCAL without tearing down the
        peer-link sessions — the Send-builds power-user dialog
        and the receiver-side housekeeping (queue_status push,
        artifact downloads for manual dispatches) keep working.
        """
        return BuildSchedulerInputs(
            remote_builds_enabled=self._remote_builds_enabled,
            pairings=dict(self._pairings),
            open_peer_links=frozenset(self._open_peer_links),
            peer_queue_status=dict(self._peer_queue_status),
        )

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
        return [self._pairing_summary_for(p) for p in self._pairings.values()]

    def _pairing_summary_for(self, pairing: StoredPairing) -> PairingSummary:
        """Project *pairing* into a wire :class:`PairingSummary`.

        Threads the live ``connected`` / ``connecting`` /
        ``last_connect_error`` state off the matching peer-link
        client handle (if any), so the snapshot path and the
        per-mutation ``_apply_pair_status_result`` response use
        one source of truth and can't drift on the dynamic
        connection-state fields. PENDING rows have no client
        handle in :attr:`_peer_link_clients` (the offloader only
        spawns a client when the receiver flips the row to
        APPROVED), so they fall through the ``handle is None``
        branch with all three fields at their connection-quiet
        defaults.
        """
        handle = self._peer_link_clients.get(pairing.pin_sha256)
        return _pairing_summary(
            pairing,
            connected=pairing.pin_sha256 in self._open_peer_links,
            connecting=handle is not None and handle.client.is_connecting,
            last_connect_error=(handle.client.last_connect_error if handle is not None else ""),
        )

    def offloader_alerts_snapshot(self) -> list[OffloaderAlertSnapshotEntry]:
        """Return the in-memory offloader pair alerts snapshot.

        Pure sync read of ``_offloader_alerts`` — same shape
        rationale as :meth:`pairings_snapshot`. The snapshot is
        ordered by insertion (Python ``dict`` insertion order
        contract); the most-recent alert lands at the end of
        the list. Frontends that want "newest first" reverse
        client-side. RAM-only state — backend restart drops
        the dict, the snapshot empties, and the frontend's
        next subscribe sees no alerts (matches the design:
        alerts describe transient detections).

        Used by
        :meth:`device_builder.DeviceBuilder._cmd_subscribe_events`
        to seed the offloader UI's alerts list. Live updates flow
        from ``OFFLOADER_PAIR_PIN_MISMATCH`` /
        ``OFFLOADER_PAIR_PEER_REVOKED`` /
        ``OFFLOADER_PAIR_ALERT_DISMISSED`` events through the
        same ``subscribe_events`` stream.
        """
        return list(self._offloader_alerts.values())

    def _dismiss_offloader_alert(self, pin_sha256: str, hostname: str, port: int) -> bool:
        """Drop the alert for *pin_sha256* and fire DISMISSED.

        Called only by the two resolution paths that fix the
        underlying broken state: :meth:`request_pair` (the user
        successfully re-paired, the alert is stale) and
        :meth:`unpair` (the user removed the row, the alert is
        moot). There is **no** operator-driven dismiss surface;
        clicking "OK got it" without acting would just hide a
        broken pairing the next peer-link session would still
        fail against, so the only ways out are re-pair or
        unpair.

        ``hostname`` / ``port`` are passed alongside the pin
        because the dismissed event still carries them as
        display fields (the frontend's alert list keys on
        ``${hostname}:${port}`` until it migrates to pin-keying
        in the follow-up frontend PR).

        Returns ``True`` when an alert was actually dropped so
        callers can avoid firing the bus event when there's no
        row to flip. The event fire keeps other subscribed tabs
        in sync with the auto-clear without re-fetching the
        snapshot.
        """
        if self._offloader_alerts.pop(pin_sha256, None) is None:
            return False
        payload: OffloaderPairAlertDismissedData = {
            "receiver_hostname": hostname,
            "receiver_port": port,
            "pin_sha256": pin_sha256,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_ALERT_DISMISSED, payload)
        return True

    def _serialize_pairings(self) -> OffloaderRemoteBuildSettings:
        """Build the on-disk shape from the in-RAM ``_pairings`` dict.

        Filters to ``status=APPROVED`` rows so a malicious LAN
        scanner can't fill the offloader's pairings file with junk
        PENDING attempts. Called by
        :meth:`Store.async_delay_save` at flush time, so the
        persisted snapshot reflects whatever's currently in RAM —
        not whatever was in RAM when the most recent mutation
        scheduled the save.

        Also persists :attr:`_remote_builds_enabled` (7b
        master toggle) so the next dashboard start picks up
        the operator's last choice without an extra mutation.
        """
        return OffloaderRemoteBuildSettings(
            pairings=[p for p in self._pairings.values() if p.status is PeerStatus.APPROVED],
            remote_builds_enabled=self._remote_builds_enabled,
        )

    # ------------------------------------------------------------------
    # Pair-status listeners — one task per PENDING StoredPairing, each
    # holding an open Noise WS to its receiver with
    # ``intent="pair_status"``. Receiver-side responder waits on
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
        listener for the row's ``pin_sha256`` already exists and
        isn't done. Cold start has no PENDING entries to spawn
        against, so this isn't called from :meth:`start`.
        """
        key = pairing.pin_sha256
        existing = self._pair_status_listeners.get(key)
        if existing is not None and not existing.done():
            return
        self._pair_status_listeners[key] = asyncio.create_task(
            self._await_pair_status_flip(pairing),
            name=f"pair-status-{pairing.receiver_hostname}:{pairing.receiver_port}",
        )

    def _cancel_pair_status_listener(self, pin_sha256: str) -> None:
        """Cancel the listener for *pin_sha256*. No-op if none running."""
        task = self._pair_status_listeners.pop(pin_sha256, None)
        if task is not None and not task.done():
            task.cancel()

    def _spawn_peer_link_client(self, pairing: StoredPairing) -> None:
        """Spawn the long-lived peer-link client for *pairing*.

        Idempotent on already-running clients — returns early if
        a client for the row's ``pin_sha256`` is still alive.
        Skips if the offloader-side identities haven't been
        loaded yet (start order: identities load before any
        spawn). Skips if the bus isn't wired (e.g. a unit test
        path).
        """
        if (
            self._offloader_dashboard_id is None
            or self._offloader_peer_link_priv is None
            or self._db.bus is None
        ):
            return
        key = pairing.pin_sha256
        existing = self._peer_link_clients.get(key)
        if existing is not None and not existing.task.done():
            return
        client = PeerLinkClient(
            receiver_hostname=pairing.receiver_hostname,
            receiver_port=pairing.receiver_port,
            identity_priv=self._offloader_peer_link_priv,
            dashboard_id=self._offloader_dashboard_id,
            # Pin the receiver's static pubkey from the
            # OOB-verified pair flow so the long-lived peer-link
            # handshake fails fast on identity drift instead of
            # admitting an attacker with their own keypair to
            # the application channel.
            pinned_static_x25519_pub=pairing.static_x25519_pub,
            pin_sha256=pairing.pin_sha256,
            receiver_label=pairing.label,
            bus=self._db.bus,
            resolver=self._peer_link_resolver,
        )
        task = asyncio.create_task(
            client.run(),
            name=f"peer-link-client-{pairing.receiver_hostname}:{pairing.receiver_port}",
        )
        self._peer_link_clients[key] = _PeerLinkClientHandle(client=client, task=task)

    def _cancel_peer_link_client(self, pin_sha256: str) -> None:
        """Cancel the peer-link client for *pin_sha256*. No-op if none running."""
        handle = self._peer_link_clients.pop(pin_sha256, None)
        if handle is not None and not handle.task.done():
            handle.task.cancel()

    def _sweep_stale_pairings_at_endpoint(
        self, hostname: str, port: int, *, keep_pin_sha256: str
    ) -> None:
        """Drop any pairing or alert at ``(hostname, port)`` whose pin isn't *keep_pin_sha256*.

        Called from :meth:`request_pair` to clean up stale rows
        when the user re-pairs against the same network
        coordinates under a fresh pin (receiver rotated
        identity, or a different receiver moved to that
        hostname). Without this sweep, the old row + its
        listener task + alert would leak under pin-keying — the
        new entry lands at the new pin, and the old pin's slot
        keeps pointing at a stale row nobody references.

        ``keep_pin_sha256`` is the pin of the row the caller is
        about to install; rows under that pin are skipped (the
        caller's own write is the source of truth).

        Walks both ``_pairings`` and ``_offloader_alerts``: an
        alert can outlive its pairing in some race paths (e.g.
        ``_apply_pair_status_result``'s pin-drift branch
        registered the alert and dropped the pairing under the
        old pin), so dropping by pin alone wouldn't cover the
        re-pair-clears-old-alert contract. Snapshots the dicts
        to lists before iterating so the in-loop pop doesn't
        mutate-during-iteration.
        """
        for stale_pin, pairing in list(self._pairings.items()):
            if stale_pin == keep_pin_sha256:
                continue
            if pairing.receiver_hostname != hostname or pairing.receiver_port != port:
                continue
            self._pairings.pop(stale_pin, None)
            self._cancel_pair_status_listener(stale_pin)
            self._cancel_peer_link_client(stale_pin)
            self._peer_queue_status.pop(stale_pin, None)
            self._open_peer_links.discard(stale_pin)
        # Alerts can outlive pairings — sweep them in a second
        # pass keyed on the alert's stored ``receiver_hostname``
        # / ``receiver_port`` (also walks the pin-keyed dict so
        # an alert under the keep_pin_sha256 stays put if the
        # user is re-confirming the same identity).
        for stale_pin, alert in list(self._offloader_alerts.items()):
            if stale_pin == keep_pin_sha256:
                continue
            if alert["receiver_hostname"] != hostname or alert["receiver_port"] != port:
                continue
            self._dismiss_offloader_alert(stale_pin, hostname, port)

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
        peer_link_identity, dashboard_identity = await self._load_offloader_identities_async()
        try:
            while True:
                try:
                    result = await peer_link_await_pair_status(
                        hostname=pairing.receiver_hostname,
                        port=pairing.receiver_port,
                        identity_priv=peer_link_identity.private_bytes,
                        dashboard_id=dashboard_identity.dashboard_id,
                        resolver=self._peer_link_resolver,
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
            key = pairing.pin_sha256
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
        # Capture diagnostic snapshot before the dict mutates
        # below — the pin_mismatch / peer_revoked events fire
        # alongside ``status="removed"`` and need the
        # offloader-side label after the row's been popped.
        label = pairing.label
        stored_pin = pairing.pin_sha256
        key = pairing.pin_sha256
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
                    self._schedule_pairings_save()
                    # Capture the alert in RAM before firing so a
                    # late-subscribing client picks it up via the
                    # ``initial_state.offloader_alerts`` snapshot.
                    pin_alert: OffloaderPinMismatchAlert = {
                        "kind": "pin_mismatch",
                        "receiver_hostname": host,
                        "receiver_port": port,
                        "pin_sha256": stored_pin,
                        "receiver_label": label,
                        "expected_pin": stored_pin,
                        "observed_pin": result.pin_sha256,
                        "fired_at": time.time(),
                    }
                    self._offloader_alerts[key] = pin_alert
                    # Fire the discriminator first so frontend
                    # subscribers get the full diagnostic payload
                    # before the row drops via the
                    # ``status_changed("removed")`` mutation. Both
                    # events ride the same global subscribe stream
                    # so order is preserved end-to-end.
                    self._fire_offloader_pair_pin_mismatch(
                        host, port, key, label, stored_pin, result.pin_sha256
                    )
                    self._fire_offloader_pair_status_changed(host, port, key, "removed")
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
            self._schedule_pairings_save()
            self._fire_offloader_pair_status_changed(host, port, key, "approved")
            # Spawn the long-lived peer-link client now that the
            # receiver has approved us. The client's
            # connect-handshake-park-reconnect loop owns the
            # session lifecycle until ``unpair`` cancels it.
            self._spawn_peer_link_client(existing)
            return True
        if result.status is IntentResponse.REJECTED:
            if self._pairings.pop(key, None) is not None:
                self._schedule_pairings_save()
                # Capture the alert in RAM before firing so a
                # late-subscribing client picks it up via the
                # ``initial_state.offloader_alerts`` snapshot.
                revoked_alert: OffloaderPeerRevokedAlert = {
                    "kind": "peer_revoked",
                    "receiver_hostname": host,
                    "receiver_port": port,
                    "pin_sha256": stored_pin,
                    "receiver_label": label,
                    "fired_at": time.time(),
                }
                self._offloader_alerts[key] = revoked_alert
                # Same fire-discriminator-first ordering as the
                # pin-mismatch branch above: subscribers see the
                # peer-revoked diagnostic before the
                # ``status_changed("removed")`` drops the row.
                self._fire_offloader_pair_peer_revoked(host, port, key, label)
                self._fire_offloader_pair_status_changed(host, port, key, "removed")
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
        pin_sha256: str,
        status: Literal["approved", "removed"],
    ) -> None:
        """Fire ``OFFLOADER_PAIR_STATUS_CHANGED`` for a pairing flip.

        Mirrors :meth:`_fire_pair_status_changed` (receiver-side)
        for shape; both methods are the named-intent boundary
        between controller logic and the bus payload format.
        ``pin_sha256`` is the canonical row identifier; receiver
        coords stay on the payload as display fields.
        """
        payload: OffloaderPairStatusChangedData = {
            "receiver_hostname": receiver_hostname,
            "receiver_port": receiver_port,
            "pin_sha256": pin_sha256,
            "status": status,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_STATUS_CHANGED, payload)

    def _fire_offloader_pair_pin_mismatch(
        self,
        receiver_hostname: str,
        receiver_port: int,
        pin_sha256: str,
        receiver_label: str,
        expected_pin: str,
        observed_pin: str,
    ) -> None:
        """Fire ``OFFLOADER_PAIR_PIN_MISMATCH`` for a drifted-pin pair_status.

        Receiver's static X25519 pubkey hash observed during the
        handshake doesn't match what the offloader stored at
        pair time on :class:`StoredPairing.pin_sha256`. Fires
        alongside ``status="removed"`` (the row drops); this
        event carries the diagnostic detail the frontend's
        4b-4 alert plumbing reshape uses to surface a "re-pair
        to confirm the new identity" CTA distinct from the
        peer-revocation path. ``pin_sha256`` is the row's
        primary key (= ``expected_pin``); duplicated as a
        separate field so the controller's listener has a
        direct lookup without parsing ``expected_pin``.
        """
        payload: OffloaderPairPinMismatchData = {
            "receiver_hostname": receiver_hostname,
            "receiver_port": receiver_port,
            "receiver_label": receiver_label,
            "pin_sha256": pin_sha256,
            "expected_pin": expected_pin,
            "observed_pin": observed_pin,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_PIN_MISMATCH, payload)

    def _fire_offloader_pair_peer_revoked(
        self,
        receiver_hostname: str,
        receiver_port: int,
        pin_sha256: str,
        receiver_label: str,
    ) -> None:
        """Fire ``OFFLOADER_PAIR_PEER_REVOKED`` for a REJECTED pair_status.

        Receiver returned ``IntentResponse.REJECTED`` on a row
        the offloader had as PENDING / APPROVED. From the
        offloader's POV all four causes (admin Reject, window
        close, identity rotation, row never existed) collapse
        to "the receiver isn't going to talk to us"; the alert
        copy stays generic. Fires alongside
        ``status="removed"``. ``pin_sha256`` is the canonical
        row identifier; receiver coords stay on the payload as
        display fields.
        """
        payload: OffloaderPairPeerRevokedData = {
            "receiver_hostname": receiver_hostname,
            "receiver_port": receiver_port,
            "receiver_label": receiver_label,
            "pin_sha256": pin_sha256,
        }
        self._db.bus.fire(EventType.OFFLOADER_PAIR_PEER_REVOKED, payload)

    # ------------------------------------------------------------------
    # Identity — surface the receiver's own dashboard_id + cert pin to
    # the Settings UI without making it reach into the cert PEM
    # directly. Rotation lives next door so the "rotate" button can
    # land in the same controller.
    # ------------------------------------------------------------------

    @api_command("remote_build/get_identity")
    async def get_identity(self, **kwargs: Any) -> IdentityView:
        """
        Return this dashboard's stable identity (id + pin + versions).

        Reads the persistent identity via
        :func:`helpers.dashboard_identity.get_or_create_identity`
        — idempotent, and lazy-creates the X25519 peer-link
        keypair if missing. ``listener_bound`` reports whether
        the peer-link Noise WS listener is currently serving
        traffic. The X25519 private key is intentionally NOT
        returned; only the SHA-256 of the public key
        (``pin_sha256``) is safe to ship, and the fingerprint
        is what an offloader pins against during pairing AND
        what the mDNS TXT advertise broadcasts. The two MUST
        match: a UI that showed one fingerprint while peers
        observed a different one on the wire would defeat the
        entire OOB-verification story.

        ``server_version`` and ``esphome_version`` ride on the
        same response so the Settings UI can render the "Build
        server" card from a single WS call instead of hopping
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
        Mint a fresh X25519 peer-link keypair, replacing whatever's on disk.

        Forces every paired offloader to re-pair: the new
        ``pin_sha256`` (SHA-256 of the new public key) is what
        peers verify against on the next Noise handshake, and
        any peer that pinned the old one will see a fingerprint
        mismatch and surface the re-pair wizard. The
        ``dashboard_id`` is preserved so the receiver-side
        audit trail stays readable across rotations.

        Side effects: (1) the bound peer-link site is torn down
        and rebuilt with the fresh X25519 key if remote-build
        is currently enabled and bound; the rebuild fail-softs
        (``listener_bound=False`` in the response) so the
        Settings UI can show "rotation succeeded but the
        listener didn't come back up — check logs". (2) The
        mDNS advertise picks up the new ``pin_sha256`` only
        when the listener was bound at rotation time: the
        TXT contract is "pin + port appear iff the listener
        is currently bound", so an unbound rotation leaves
        mDNS alone and the next successful bind (after the
        operator flips remote-build on, or after the
        fail-soft path resolves) advertises the new pin.
        (3) An :attr:`EventType.REMOTE_BUILD_IDENTITY_ROTATED`
        event fires on the bus carrying
        ``{dashboard_id, pin_sha256}`` regardless of
        listener-bound state — subscribers (the offloader-side
        peer-link, the receiver Settings UI) refresh their
        cached pin without polling ``get_identity`` even when
        the listener didn't come back up.

        **Concurrent calls fail with ``ALREADY_EXISTS``.** Two
        rotations racing would each tear down + rebuild the
        listener, and back-to-back rotation is almost always an
        accidental double-click rather than two intentional
        events; the frontend is expected to confirm before each
        call. Rotation is otherwise intentionally cheap to
        invoke (X25519 keygen + one atomic file write), bounded
        only by the WS auth gate on this command's channel.
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
                None, _dashboard_identity_helper.rotate_identity, self._db.settings.config_dir
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
    # Peer CRUD — receiver-UI surface for the Pairing requests inbox
    # and the approved-peers list. The peer-link listener is the
    # actual creator of PENDING rows; these commands are the
    # receiver-side admin's UI surface for acting on them.
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
          surface a ``peer_revoked`` UI alert.

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
        return self._to_view(await self._load_settings_async())

    # ------------------------------------------------------------------
    # Peer-link Noise WS dispatch helpers — called by the post-handshake
    # intent dispatcher in :mod:`controllers.remote_build_peer_link`.
    # These methods own the storage / event-firing side; the dispatcher
    # owns the wire side.
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

        # Security: refuse to overwrite a PENDING entry's pubkey.
        # The retry case the comment below describes is legitimate
        # only when the *same* offloader sends a second
        # ``pair_request`` — same X25519 keypair, possibly different
        # label / peer_ip / paired_at. A different pubkey under the
        # same dashboard_id is one of:
        #   1. A second peer claiming the same dashboard_id (the
        #      offloader-generated ``dashboard_id`` has 22 chars of
        #      base64url entropy; collision is astronomical and
        #      indistinguishable from impersonation regardless).
        #   2. The offloader rotating its identity mid-pair-request.
        #      Identity rotation preserves ``dashboard_id`` (see
        #      ``rotate_identity`` in ``helpers/dashboard_identity.py``),
        #      so the rotated-then-retried case is theoretically
        #      reachable; rejecting forces the operator to abandon
        #      the stale PENDING row and start over with a fresh
        #      pair_request, which is the right user-visible outcome.
        #   3. A LAN-adjacent attacker that read the legitimate
        #      offloader's broadcast ``dashboard_id`` off mDNS and
        #      sent a ``pair_request`` with their own pubkey.
        #
        # Pre-fix, case (3) silently overwrote the operator's PENDING
        # row with the attacker's pubkey. Two damage modes:
        #   * Pairing DoS: an attacker flickers the row between
        #     legitimate and attacker pubkeys, the inbox keeps
        #     re-rendering, the operator can't reliably approve
        #     the right row.
        #   * Approve-without-verifying impersonation: if the
        #     operator clicks Approve based on muscle memory
        #     without re-comparing the on-screen fingerprint to
        #     their OOB-known one, they approve the attacker's
        #     pubkey. Paired peers can ``submit_job``, which the
        #     receiver compiles and serves binaries back for.
        #
        # **No practical attack is exploited in the wild today** —
        # the OOB fingerprint check that operators perform during
        # the approve step is the actual security gate, and a
        # vigilant operator catches the swap. This fix is
        # defense-in-depth: closing the silent-overwrite path
        # turns the impersonation chain from "operator must
        # OOB-verify carefully" into "attacker cannot inject a
        # rival pubkey into an active PENDING row at all". The
        # DoS variant — which doesn't depend on operator
        # mistakes — is closed unconditionally.
        #
        # Same-pubkey retries still refresh ``label`` / ``peer_ip``
        # / ``paired_at`` (the legitimate retry case below). The
        # check is RAM-only; no disk hop, no UI event (firing a
        # conflict event would only advertise the attempt to an
        # attacker and let them noise up the inbox by triggering
        # it on demand — refusing silently is strictly better).
        existing = self._pending_peers.get(dashboard_id)
        if existing is not None and existing.static_x25519_pub != static_x25519_pub:
            _LOGGER.warning(
                "pair_request from %s claims dashboard_id=%s but presented "
                "a different X25519 pubkey than the existing PENDING entry "
                "from %s; refusing the overwrite",
                peer_ip,
                dashboard_id,
                existing.peer_ip,
            )
            return IntentResponse.REJECTED

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
          application messages.
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
    # Pairing window — in-process deadline that gates
    # ``intent="pair_request"`` Noise frames at the listener (the
    # listener consumes :meth:`is_pairing_window_open`). See issue
    # #106 design choice (c).
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

        Consumed by the peer-link listener to gate
        ``intent="pair_request"`` Noise frames. A closed window
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
