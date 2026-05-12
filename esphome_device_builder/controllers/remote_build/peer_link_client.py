"""
Offloader-side peer-link Noise WS client (issue #106).

Initiator counterpart of
:mod:`controllers.remote_build_peer_link`'s responder. Opens a
``ws://<receiver>:<peer_link_port>/remote-build/peer-link``
WebSocket, drives the three Noise XX handshake messages from the
offloader side, optionally exchanges application-level
``intent`` / ``intent_response`` framing, and surfaces the
captured receiver static pubkey hash to the caller.

This module is the wire-shape twin of
``remote_build_peer_link.py``: same handshake, opposite role.
The two share the cipher suite + frame layout via
:mod:`helpers.peer_link_noise` (single :class:`PeerLinkNoiseSession`
class, ``initiator`` / ``responder`` factories) and the same
exception-tuple (:data:`helpers.peer_link_noise.NOISE_ERRORS`)
so a future ``noiseprotocol`` upgrade only has to thread through
one place.

The wire-flow shape — TCP connect, 3 Noise XX messages, post-
handshake transport frame, error mapping — is identical across
every initiator-side intent the offloader needs (``preview``,
``pair_request``, ``pair_status``, ``peer_link``); only the
msg3 payload and which response codes count as success differ.
:func:`drive_initiator_round_trip` owns the shared flow; each
public ``preview_pair`` / ``request_pair`` /
``await_pair_status`` function is a thin wrapper that provides
the intent + msg3 payload + accepted-response set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

import aiohttp
from yarl import URL

from ...helpers import json as _json
from ...helpers.peer_link_bundle import (
    BUNDLE_CHUNK_SIZE_BYTES,
    FIRMWARE_MAX_TOTAL_BYTES,
    BundleAssembler,
    BundleAssemblerError,
    chunk_bundle,
    compute_bundle_sha256,
    decode_chunk,
    encode_chunk,
)
from ...helpers.peer_link_frames import frame_schema, is_valid_frame
from ...helpers.peer_link_noise import (
    NOISE_ERRORS,
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from ...helpers.peer_link_resolver import make_peer_link_http_session
from ...models import (
    PAIRING_VERSION_MAX_LEN,
    ArtifactsChunkFrameData,
    ArtifactsEndFrameData,
    ArtifactsStartFrameData,
    CancelJobFrameData,
    DownloadArtifactsFrameData,
    EventType,
    IntentResponse,
    JobOutputFrameData,
    JobStateChangedFrameData,
    OffloaderJobOutputData,
    OffloaderJobStateChangedData,
    OffloaderPairPinMismatchData,
    OffloaderPeerLinkClosedData,
    OffloaderPeerLinkOpenedData,
    OffloaderQueueStatusChangedData,
    PeerLinkIntent,
    SubmitJobAckFrameData,
    SubmitJobChunkFrameData,
    SubmitJobFrameData,
)
from ._client_models import (
    DownloadArtifactsError,
    DownloadArtifactsResult,
    InitiatorRoundTrip,
    PairStatusResult,
    PeerLinkClientError,
    PeerLinkNoSessionError,
    RequestPairResult,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
    _DownloadArtifactsState,
    _SessionLoopState,
)
from .peer_link import (
    APP_FRAME_MAX_BYTES,
    PEER_LINK_PATH,
    AppMessageType,
    PeerLinkChannel,
    TerminateReason,
    run_peer_link_heartbeat,
)

if TYPE_CHECKING:
    from aiohttp.resolver import AbstractResolver

    from ...helpers.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)

# Flat tuple of every exception class that can land out of the
# decrypt + JSON-parse step. Built once at module level rather
# than inlined as ``(*NOISE_ERRORS, _json.JSONDecodeError)`` in
# the ``except`` clause so mypy can verify the type without
# tripping on its star-unpack-in-except limitation (the runtime
# would handle the inline form fine on Python 3.12+, but the
# type checker can't follow it).
_RESPONSE_DECODE_ERRORS: tuple[type[Exception], ...] = (
    *NOISE_ERRORS,
    _json.JSONDecodeError,
)


# Total budget for one initiator round-trip: TCP connect + WS
# upgrade + 3 Noise messages + post-handshake response + clean
# close. Bounded by LAN latency + the receiver's own per-step
# timeout (10s in
# ``remote_build_peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS``);
# 10s here matches that budget so we don't give up before the
# receiver does, but doesn't pin a coroutine forever if the
# remote side is gone.
_DEFAULT_TIMEOUT_SECONDS = 10.0


# Total budget for one ``intent="pair_status"`` round-trip.
# Receiver-side ``lookup_peer_for_status`` parks indefinitely on
# its bus listener — there's no internal timeout, the connection
# stays open until either an admin click flips the row or the
# receiver-side pairing window closes (firing ``status="removed"``
# events that wake the wait). The pairing window's default
# lifetime is ``_PAIRING_WINDOW_DURATION_SECONDS`` = 300s but
# extends on user activity, so the receiver-side wait can
# legitimately span tens of minutes. Pick a client-side total an
# order of magnitude above the default window so a typical
# "admin opens screen, walks away to verify, comes back, clicks
# Accept" flow doesn't trip the offloader's ``aiohttp`` timeout
# and force a reconnect (which would itself land back on the
# same wait, just with a Noise handshake of churn). When the
# offloader process actually wants to give up — controller stop,
# unpair — the listener task is cancelled directly and the WS
# closes via the cancellation, not via this timeout.
_PAIR_STATUS_TIMEOUT_SECONDS = 3600.0


# Hard cap on a single inbound WS frame for the *control-plane*
# round-trip driven by :func:`drive_initiator_round_trip`. Each
# receiver response on this path is a Noise-encrypted JSON object
# with a small fixed shape (status code, pubkey hash, optional
# label); well under 1 KiB in practice. aiohttp's default
# ``max_msg_size`` is 4 MiB, which is wildly generous here: a
# malicious or buggy receiver could otherwise spend ~4 MiB of
# offloader memory + Noise-decrypt + JSON-parse CPU per round-
# trip. 64 KiB is two orders of magnitude above the realistic
# max while still giving aiohttp a reasonable header-and-frame
# slack.
#
# This cap explicitly does NOT apply to the firmware-bytes
# ``peer_link`` intent (issue #106). That payload is megabytes
# of compiled firmware and uses a separate streaming driver —
# Noise has a hard 65535-byte ciphertext frame limit, so the
# firmware path reads many small frames and streams them to
# disk rather than a single ``receive_bytes()`` call. The
# streaming driver tunes its own ``max_msg_size`` to one Noise
# frame (~64 KiB + slack); this constant stays scoped to the
# JSON status responses.
_CONTROL_RESPONSE_MAX_BYTES = 64 * 1024


def _extract_receiver_esphome_version(response: dict[str, Any]) -> str:
    """Lift ``esphome_version`` off the post-handshake response.

    The receiver populates the field on every ``intent_response``
    payload (see :func:`controllers.remote_build.peer_link._send_response`)
    so the offloader can land it on
    :attr:`StoredPairing.esphome_version` and pick_build_path's
    version-compat gate can read it without an extra round-trip.

    Returns:
        The receiver's ``esphome.const.__version__`` as a string,
        or ``""`` when the field is missing (older receiver
        predating this wire change), not a string (malformed
        response from a buggy peer), or exceeds
        :data:`PAIRING_VERSION_MAX_LEN` (a malicious / buggy
        peer trying to poison the sidecar — the
        :class:`StoredPairing` validator caps at the same length
        on disk-load, so a longer value would persist through
        the in-memory mutation path and then fail the next load
        of the persisted sidecar). The cap mirrors the validator
        so the wire seam and the disk seam can't drift apart.

    Empty flows through as "unknown" — pick_build_path's gate
    treats unknown as compatible (silent-fallback semantic; the
    operator opted in to remote builds, refusing on the unknown
    case would be more surprising than the alternative).
    """
    value = response.get("esphome_version", "")
    if not isinstance(value, str):
        return ""
    if len(value) > PAIRING_VERSION_MAX_LEN:
        return ""
    return value


def _build_ws_url(hostname: str, port: int) -> URL:
    """Build the peer-link WS URL for *hostname* / *port*.

    Uses :class:`yarl.URL` (already in our dep closure via aiohttp)
    rather than hand-rolled f-string + ``urllib.parse.quote``:

    * IPv6 literals get auto-bracketed (``::1`` →
      ``ws://[::1]:6055/...``); the f-string version would have
      produced an unparsable URL.
    * Pathological characters in the hostname (slash, query
      terminators, fragment markers, embedded ``:port``) raise
      ``ValueError`` loudly instead of getting silently
      percent-encoded into a non-resolvable form. The
      WS-command boundary's ``_validate_hostname`` already
      defers to :class:`yarl.URL.build` for the URL-correctness
      check and rejects these shapes as ``INVALID_ARGS``, so
      the validator and ``_build_ws_url`` share a single source
      of truth on what a host is. A future caller that bypasses
      ``_validate_hostname`` would still get the ``ValueError``
      here; :func:`drive_initiator_round_trip` keeps a
      defense-in-depth catch that maps it to
      :class:`PeerLinkClientError` (→ UNAVAILABLE) so the
      surface contract holds even on the bypass path.
    * Path is given to yarl as a constant; encoding stays
      intact across versions.

    The receiver listens on plain TCP — Noise XX provides the
    transport security — so the scheme is ``ws://`` not
    ``wss://``. Returns a :class:`URL` because
    :meth:`aiohttp.ClientSession.ws_connect` accepts both
    strings and ``URL`` instances; passing the typed shape
    skips one re-parse on the aiohttp side.
    """
    return URL.build(scheme="ws", host=hostname, port=port, path=PEER_LINK_PATH)


async def _drive_initiator_handshake_and_read_response(
    *,
    ws: aiohttp.ClientWebSocketResponse,
    sess: PeerLinkNoiseSession,
    intent: PeerLinkIntent,
    msg3_payload: bytes,
    read_timeout_seconds: float,
) -> bytes:
    """Drive Noise XX msg1/msg2/msg3 + read the post-handshake response ciphertext.

    Shared by :func:`drive_initiator_round_trip` (short-lived
    intents — preview / pair_request / pair_status) and
    :meth:`PeerLinkClient._run_one_session` (long-lived
    ``peer_link`` intent). Pre: *ws* is connected; *sess* is a
    fresh initiator. Post: *sess* is in transport mode. Returns
    the encrypted post-handshake response bytes; the caller is
    responsible for decrypting and parsing them.

    Each receive is bounded by *read_timeout_seconds* via
    :func:`asyncio.wait_for` so a stalled peer fails fast even
    when the surrounding WS session has no session-wide timeout
    (the long-lived peer-link client deliberately drops
    ``ClientTimeout(total=...)`` so the dispatch loop can stay
    parked indefinitely).
    """
    msg1 = _json.dumps({"intent": intent.value})
    await ws.send_bytes(sess.write_handshake_message(msg1))
    sess.read_handshake_message(
        await asyncio.wait_for(ws.receive_bytes(), timeout=read_timeout_seconds)
    )
    await ws.send_bytes(sess.write_handshake_message(msg3_payload))
    return await asyncio.wait_for(ws.receive_bytes(), timeout=read_timeout_seconds)


async def drive_initiator_round_trip(
    *,
    hostname: str,
    port: int,
    identity_priv: bytes,
    intent: PeerLinkIntent,
    msg3_payload: bytes = b"",
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    resolver: AbstractResolver | None = None,
) -> InitiatorRoundTrip:
    """Run one Noise XX round-trip from the initiator side.

    The flow is identical for every offloader-side intent
    (``preview`` / ``pair_request`` / ``pair_status`` /
    ``peer_link``); callers vary only the *intent* discriminator
    (cleartext on msg1) and the encrypted *msg3_payload* (carries
    ``label`` + ``dashboard_id`` for ``pair_request`` and
    similar). The shared driver here keeps the connect / send /
    receive / decode / error-map plumbing in one place so future
    intents don't reinvent it.

    Wire shape, mirroring the receiver-side responder in
    :func:`controllers.remote_build_peer_link._drive_peer_link_session`:

    * msg1 — send ``{"intent": "..."}`` cleartext-but-noise-framed
      (msg1's payload is plaintext on the wire per Noise XX;
      coarse intent only, no sensitive fields).
    * msg2 — receive the responder's ephemeral + static; the
      library's read-message places ``static_x25519_pub`` into
      our handshake state.
    * msg3 — send our static + the *msg3_payload* (encrypted
      under the now-mixed cipher).
    * Post-handshake — receive one transport frame carrying
      ``{"intent_response": "..."}``; decrypt + JSON-parse.

    Raises :class:`PeerLinkClientError` on any transport,
    handshake, or decode failure with the underlying exception
    attached as ``__cause__`` for log inspection. The caller is
    responsible for branching on
    :attr:`InitiatorRoundTrip.intent_response` (each intent has
    its own accept-set).
    """
    sess = PeerLinkNoiseSession.initiator(identity_priv)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    label = f"peer-link {intent.value} to {hostname}:{port}"

    # ``_build_ws_url`` is inside the try block as defense-in-depth.
    # The WS-command boundary's ``_validate_hostname`` defers to
    # :class:`yarl.URL.build` and already rejects
    # path-injection-shaped hosts (slash, ``?``, ``#``, ``@``,
    # embedded ``:port``) as ``INVALID_ARGS``, so on the
    # validator-gated path :meth:`URL.build` here will never
    # raise. But a future caller that calls this driver
    # directly without going through the validator (e.g. a
    # 4a-o part 3/4 helper that takes a stored ``hostname``
    # off disk and assumes it's clean) would otherwise see the
    # ``ValueError`` escape this function and surface as
    # ``INTERNAL_ERROR`` instead of the documented
    # ``UNAVAILABLE`` mapping. Wrapping the build inside the
    # try keeps the contract holding regardless of which entry
    # point the caller used.
    try:
        url = _build_ws_url(hostname, port)
        async with (
            make_peer_link_http_session(timeout=timeout, resolver=resolver) as http,
            http.ws_connect(url, max_msg_size=_CONTROL_RESPONSE_MAX_BYTES) as ws,
        ):
            response_ct = await _drive_initiator_handshake_and_read_response(
                ws=ws,
                sess=sess,
                intent=intent,
                msg3_payload=msg3_payload,
                read_timeout_seconds=timeout_seconds,
            )
    except (TimeoutError, aiohttp.ClientError, OSError, ValueError, TypeError) as exc:
        msg = f"{label} failed: {exc}"
        _LOGGER.debug(msg, exc_info=True)
        raise PeerLinkClientError(msg) from exc
    except NOISE_ERRORS as exc:
        msg = f"{label} Noise handshake failed: {exc}"
        _LOGGER.warning(msg, exc_info=True)
        raise PeerLinkClientError(msg) from exc

    try:
        decoded = _json.loads(sess.decrypt(response_ct))
    except _RESPONSE_DECODE_ERRORS as exc:
        msg = f"{label} response decode failed: {exc}"
        _LOGGER.warning(msg, exc_info=True)
        raise PeerLinkClientError(msg) from exc

    if not isinstance(decoded, dict):
        msg = f"{label} response was not a JSON object: {decoded!r}"
        raise PeerLinkClientError(msg)
    intent_response = decoded.get("intent_response")
    if not isinstance(intent_response, str):
        msg = f"{label} response missing 'intent_response' string: {decoded!r}"
        raise PeerLinkClientError(msg)

    try:
        remote_static = sess.remote_static_pub
    except HandshakeNotCompleteError as exc:
        msg = f"{label} handshake completed without capturing remote static pubkey"
        raise PeerLinkClientError(msg) from exc

    return InitiatorRoundTrip(
        intent_response=intent_response,
        remote_static_pub=remote_static,
        response=decoded,
    )


async def preview_pair(
    *,
    hostname: str,
    port: int,
    identity_priv: bytes,
    resolver: AbstractResolver | None = None,
) -> str:
    """Run an ``intent="preview"`` round-trip; return the receiver's pin_sha256.

    Thin wrapper around :func:`drive_initiator_round_trip`:
    preview's accept-set is just ``IntentResponse.OK`` (anything
    else is a receiver-side bug or a misconfigured deployment);
    its msg3 payload is empty (the receiver already has what it
    needs from msg2 from the offloader's perspective).

    The frontend renders the returned ``pin_sha256`` for the
    user to OOB-verify against the receiver's "Build server"
    Settings card; only after that confirmation does the
    offloader call ``request_pair``.
    """
    rt = await drive_initiator_round_trip(
        hostname=hostname,
        port=port,
        identity_priv=identity_priv,
        intent=PeerLinkIntent.PREVIEW,
        resolver=resolver,
    )
    if rt.intent_response != IntentResponse.OK.value:
        msg = f"peer-link preview rejected with intent_response={rt.intent_response!r}"
        raise PeerLinkClientError(msg)
    return pin_sha256_for_pubkey(rt.remote_static_pub)


async def request_pair(
    *,
    hostname: str,
    port: int,
    identity_priv: bytes,
    label: str,
    dashboard_id: str,
    resolver: AbstractResolver | None = None,
) -> RequestPairResult:
    """Run an ``intent="pair_request"`` round-trip; return the receiver's response.

    Thin wrapper around :func:`drive_initiator_round_trip`:
    sends ``{"label": ..., "dashboard_id": ...}`` in the
    encrypted msg3 payload (per the Noise XX wire spec, msg3 is
    encrypted under the now-finalized cipher — safe for the
    offloader-side identity metadata) and returns the
    receiver's ``intent_response`` alongside the receiver's
    captured pubkey.

    The caller is responsible for the TOCTOU pin check:
    compare the returned :attr:`RequestPairResult.pin_sha256`
    against the value the user OOB-confirmed in
    ``preview_pair`` *before* persisting any state. The driver
    here completes the handshake regardless because the
    receiver doesn't expose its pubkey otherwise — the check
    has to happen post-handshake on the offloader side. A
    mismatch + bail-after-handshake leaks no information to
    the receiver beyond the fact that the offloader requested
    pairing (which is also true on the no-mismatch path).

    Maps ``IntentResponse`` strings the receiver may return —
    ``REJECTED`` / ``NO_PAIRING_WINDOW`` / ``PENDING`` /
    ``APPROVED`` — back to the typed enum. An unknown wire
    value (e.g. a future receiver protocol bump) raises
    :class:`PeerLinkClientError`; the WS-command layer above
    should treat that as ``UNAVAILABLE``.
    """
    msg3_payload = _json.dumps({"label": label, "dashboard_id": dashboard_id})
    rt = await drive_initiator_round_trip(
        hostname=hostname,
        port=port,
        identity_priv=identity_priv,
        intent=PeerLinkIntent.PAIR_REQUEST,
        msg3_payload=msg3_payload,
        resolver=resolver,
    )
    try:
        status = IntentResponse(rt.intent_response)
    except ValueError as exc:
        msg = f"peer-link pair_request: unknown intent_response={rt.intent_response!r}"
        raise PeerLinkClientError(msg) from exc
    return RequestPairResult(
        status=status,
        pin_sha256=pin_sha256_for_pubkey(rt.remote_static_pub),
        remote_static_pub=rt.remote_static_pub,
    )


async def await_pair_status(
    *,
    hostname: str,
    port: int,
    identity_priv: bytes,
    dashboard_id: str,
    resolver: AbstractResolver | None = None,
) -> PairStatusResult:
    """Run an ``intent="pair_status"`` long-poll round-trip.

    Used by the offloader's pair-status listener tasks (phase
    4a-o part 4) to ask the receiver "has my pending row
    flipped status yet?" with sub-second latency on the
    happy path.

    Receiver-side semantics: if the snapshot is APPROVED or
    REJECTED, returns immediately. If PENDING, the receiver
    holds the response open indefinitely (no timeout) while
    parking on its own bus's
    :attr:`EventType.REMOTE_BUILD_PAIR_STATUS_CHANGED` event
    for the matching ``dashboard_id``. Window-close clears the
    receiver's pending dict and fires removed events for each
    cleared entry, which wakes the wait and re-snapshots to
    REJECTED (no row matches anymore) — the caller's listener
    treats this the same as an admin Reject.

    Client-side total budget is
    :data:`_PAIR_STATUS_TIMEOUT_SECONDS` (~1h),
    deliberately set well above the receiver's 5-min default
    pairing-window lifetime so a typical "admin opens screen,
    walks away to verify pin, comes back, clicks Accept" flow
    doesn't trip the offloader's ``aiohttp`` timeout. The
    listener task that owns the call is cancelled directly on
    ``unpair`` / controller stop, so this timeout only fires
    if a receiver-side process becomes wedged for an hour.

    Wire shape: the encrypted msg3 carries
    ``{"dashboard_id": dashboard_id}``. The receiver doesn't
    need any other field — the row already exists, the pin is
    captured from the handshake transcript, and there's no
    ``label`` to update on a status query.

    Caller is responsible for the pin-drift check: compare
    :attr:`PairStatusResult.pin_sha256` against the stored
    :attr:`models.StoredPairing.pin_sha256`. A mismatch means
    the receiver rotated identity since pair time; the caller
    should treat that as a peer-revoked signal (drop the local
    row + fire ``status="removed"``) rather than persisting a
    silently-substituted pubkey.

    Maps unknown ``intent_response`` strings to
    :class:`PeerLinkClientError`; the WS-command layer treats
    that as ``UNAVAILABLE`` (transient receiver protocol bug,
    not a confirmed peer-revoked signal).
    """
    msg3_payload = _json.dumps({"dashboard_id": dashboard_id})
    rt = await drive_initiator_round_trip(
        hostname=hostname,
        port=port,
        identity_priv=identity_priv,
        intent=PeerLinkIntent.PAIR_STATUS,
        msg3_payload=msg3_payload,
        timeout_seconds=_PAIR_STATUS_TIMEOUT_SECONDS,
        resolver=resolver,
    )
    try:
        status = IntentResponse(rt.intent_response)
    except ValueError as exc:
        msg = f"peer-link pair_status: unknown intent_response={rt.intent_response!r}"
        raise PeerLinkClientError(msg) from exc
    return PairStatusResult(
        status=status,
        pin_sha256=pin_sha256_for_pubkey(rt.remote_static_pub),
    )


# ---------------------------------------------------------------------------
# Long-lived offloader-side peer-link session.
# ---------------------------------------------------------------------------


# Auto-reconnect cadence after a session ends. Initial 1-second
# wait keeps a transient drop (LAN flap, brief receiver restart)
# from looking like a hang to the user; the 30s cap keeps an
# extended outage from spamming the receiver's accept queue.
# Reset to the initial value on every successful connect so a
# flaky path doesn't permanently degrade to the cap.
_RECONNECT_INITIAL_BACKOFF_SECONDS = 1.0
_RECONNECT_MAX_BACKOFF_SECONDS = 30.0


# Offloader-side close reasons that aren't on the wire (the
# wire-level reasons live in :class:`TerminateReason` — those
# come *from* the receiver). These describe close paths that
# originate on our side: transport error, our own heartbeat
# timeout, controller-initiated stop. Surfaced verbatim in the
# ``OFFLOADER_PEER_LINK_CLOSED`` event payload's ``reason``
# field so subscribers can distinguish "we lost the connection"
# from "the receiver kicked us."
_LOCAL_CLOSE_TRANSPORT_ERROR = "transport_error"
_LOCAL_CLOSE_HEARTBEAT_TIMEOUT = "heartbeat_timeout"
_LOCAL_CLOSE_CLIENT_STOPPED = "client_stopped"
_LOCAL_CLOSE_PEER_HUNG_UP = "peer_hung_up"
_LOCAL_CLOSE_AUTH_REJECTED = "auth_rejected"
# Receiver's static X25519 pubkey hash (from the live Noise XX
# handshake) didn't match the value the offloader OOB-confirmed
# at pair time. Either the receiver's identity legitimately
# rotated, or an attacker has interposed (e.g. mDNS spoof
# pointing the offloader at an attacker-controlled host that
# completed the handshake with its own keypair). The
# :class:`PeerLinkClient` aborts the connection before any
# application frames flow and orphans itself so the reconnect
# loop doesn't hammer the wrong endpoint; the operator's
# resolution is to re-pair (clearing the alert) or unpair
# (removing the row).
_LOCAL_CLOSE_PIN_MISMATCH = "pin_mismatch"


# How long :meth:`PeerLinkClient.submit_job` waits for the
# receiver's ``submit_job_ack`` after the last chunk goes out.
# Sized for the receiver's worst-case
# bundle-finalise + extract + queue-acquire path: SHA-256 over
# 4 MiB (capped at :data:`BUNDLE_MAX_TOTAL_BYTES`) is sub-100ms
# even on a Raspberry Pi class SoC, ``prepare_bundle_for_compile``
# walks the tar entries (a few hundred files, low-MiB), and the
# firmware queue's lock contention is bounded by the size of an
# individual ``_enqueue`` call. 60s gives generous headroom for
# a busy receiver under disk-IO contention without letting a
# silently-dead session pin the offloader's submit handler
# forever. Mismatch with no ack arriving inside the window
# raises :class:`SubmitJobTimeoutError` and the WS command
# surfaces a structured error to the caller.
_SUBMIT_JOB_ACK_TIMEOUT_SECONDS = 60.0


# Voluptuous schemas for the peer-supplied inbound wire frames
# the offloader receive loop dispatches into bus events / ack
# futures / download assemblers. Built via
# :func:`helpers.peer_link_frames.frame_schema` so the
# ``bool``-vs-``int`` special case (Python's
# ``isinstance(True, int) is True``) is handled the same way
# every shared frame schema in the project does. Optional
# fields (``SubmitJobAckFrameData.reason`` /
# ``ArtifactsEndFrameData.reason``) live outside the schema —
# the dispatch reads ``frame.get("reason")`` post-validate.
_SUBMIT_JOB_ACK_SCHEMA = frame_schema({"job_id": str, "accepted": bool})

_JOB_STATE_CHANGED_SCHEMA = frame_schema({"job_id": str, "status": str, "error_message": str})

_JOB_OUTPUT_SCHEMA = frame_schema({"job_id": str, "stream": str, "line": str})

_QUEUE_STATUS_SCHEMA = frame_schema({"idle": bool, "running": bool, "queue_depth": int})

# Schemas for the 6a artifact-download stream frames.
_ARTIFACTS_START_SCHEMA = frame_schema(
    {
        "job_id": str,
        "total_bytes": int,
        "num_chunks": int,
        "artifacts_sha256": str,
        "firmware_offset": str,
    }
)

_ARTIFACTS_CHUNK_SCHEMA = frame_schema(
    {
        "job_id": str,
        "chunk_index": int,
        "data_b64": str,
        "is_last": bool,
    }
)

_ARTIFACTS_END_SCHEMA = frame_schema({"job_id": str, "accepted": bool})

# Allowed ``status`` values on inbound ``job_state_changed``
# frames, mirroring :class:`JobStateChangedFrameData`'s
# ``Literal``. Membership check after the str-shape gate so a
# misbehaving receiver sending ``status="unknown"`` is dropped
# at the wire layer instead of fanning out a malformed bus
# event for downstream consumers.
_JOB_STATE_CHANGED_VALID_STATUS: frozenset[str] = frozenset(
    {"queued", "running", "completed", "failed", "cancelled"}
)

# Allowed ``stream`` values on inbound ``job_output`` frames,
# mirroring :class:`JobOutputFrameData`'s ``Literal``.
_JOB_OUTPUT_VALID_STREAM: frozenset[str] = frozenset({"stdout", "stderr"})


class PeerLinkClient:
    """
    Long-lived offloader-side peer-link Noise WS session.

    One instance per APPROVED :class:`StoredPairing`, owned by
    :class:`OffloaderController`. Drive via
    :meth:`run` (cancellable asyncio task) — connects to the
    receiver's peer-link port, runs the Noise XX handshake with
    ``intent="peer_link"``, parks on a receive loop, drives an
    encrypted heartbeat, and reconnects on any close other than
    a receiver-side ``superseded`` (which would loop forever
    against whatever instance now holds our slot).

    Bus events fire on every transition: ``OFFLOADER_PEER_LINK_OPENED``
    once the post-handshake ``intent_response: ok`` lands and
    the dispatch loop is parked, and ``OFFLOADER_PEER_LINK_CLOSED``
    on every clean exit (carries a ``reason`` so the offloader-
    side frontend Settings UI can branch on close cause).

    Cancelling the :meth:`run` task is the controller-side
    teardown path — the run loop's ``finally`` chain sends a
    ``terminate{reason: client_stopped}`` to the receiver before
    the WS closes so the receiver-side session loop unwinds
    cleanly without waiting for its heartbeat to time out.
    """

    def __init__(
        self,
        *,
        receiver_hostname: str,
        receiver_port: int,
        identity_priv: bytes,
        dashboard_id: str,
        pinned_static_x25519_pub: bytes,
        pin_sha256: str,
        receiver_label: str,
        bus: EventBus,
        resolver: AbstractResolver | None = None,
    ) -> None:
        self._hostname = receiver_hostname
        self._port = receiver_port
        self._identity_priv = identity_priv
        self._dashboard_id = dashboard_id
        # Shared :class:`aiohttp` resolver wired to the
        # dashboard's :class:`AsyncZeroconf` so ``.local``
        # receiver hostnames resolve through mDNS instead of the
        # OS resolver (which often doesn't have mDNS plumbed).
        # ``None`` falls back to ``aiohttp``'s default resolver,
        # which is the only viable shape for unit tests that
        # don't construct a real Zeroconf.
        self._resolver = resolver
        # Pinned receiver pubkey from the OOB-verified pair flow,
        # captured during ``preview_pair`` and stored on
        # :class:`StoredPairing.static_x25519_pub`. Compared
        # against ``session.remote_static_pub`` post-handshake on
        # every connect so an attacker with their own X25519
        # keypair can't complete Noise XX against this client and
        # reach the application channel. ``pin_sha256`` is the
        # SHA-256 of the same pubkey, carried on every event the
        # client fires so the controller's listener can key into
        # ``_open_peer_links`` / ``_offloader_alerts`` /
        # ``_peer_queue_status`` (pin-keyed offloader state).
        # ``receiver_label`` is carried so
        # the pin-mismatch alert can name the row at firing time.
        self._pinned_static_x25519_pub = pinned_static_x25519_pub
        self._pin_sha256 = pin_sha256
        self._receiver_label = receiver_label
        self._bus = bus
        # Set to True when we observe a receiver-side
        # ``terminate{reason: superseded}`` close — means a
        # newer offloader instance with the same dashboard_id
        # has taken our slot. Reconnecting would just collide
        # with that instance and trigger an endless flap, so
        # we orphan the run loop instead. The controller can
        # explicitly :meth:`run` again (e.g. after a config
        # reload) to reset.
        self._orphaned = False
        # Set ``True`` once a session reached
        # ``intent_response: ok`` and the dispatch loop parked.
        # The reconnect-backoff logic in :meth:`run` resets the
        # backoff window only when the previous session opened —
        # if we never got past the handshake (transport error,
        # auth rejected) the backoff advances exponentially so a
        # broken receiver doesn't get hammered.
        self._session_was_opened = False
        # Live :class:`PeerLinkChannel` for the currently-open
        # session, or ``None`` when between sessions. Set inside
        # :meth:`_run_session_loops` before the receive loop
        # parks, cleared in the same method's ``finally`` after
        # the loop exits. :meth:`submit_job` reads this to know
        # whether a session is live (raising
        # :class:`PeerLinkNoSessionError` if not) and to drive
        # the chunk send through the same channel the receive
        # loop is parked on. Only one writer (the run task) and
        # one reader (the controller's WS submit handler), both
        # on the same event loop, so no lock is needed.
        self._active_channel: PeerLinkChannel | None = None
        # Per-job ack futures, keyed on the ``job_id`` we put on
        # the ``submit_job`` header. Populated by
        # :meth:`submit_job` before the header goes out, drained
        # by the receive loop on the matching ``submit_job_ack``
        # frame, and force-completed in
        # :meth:`_run_session_loops`'s ``finally`` if the session
        # closes mid-flow (so ``submit_job`` doesn't hang on the
        # ack timeout when the wire is already gone). Future's
        # ``set_result`` value is the validated ack frame; on
        # session-loss the future gets
        # :class:`SubmitJobSessionLostError`.
        self._submit_job_acks: dict[str, asyncio.Future[SubmitJobAckFrameData]] = {}
        # Last-connection-failure description for the operator-
        # facing "Last connection error" line on the paired-rows
        # list. Populated in :meth:`_run_one_session`'s exception
        # paths with ``f"{type(exc).__name__}: {exc}"`` for
        # transport / Noise failures, ``"auth rejected"`` for the
        # post-handshake intent_response branch, and
        # ``"pin mismatch"`` for the orphan-on-rotation path.
        # Cleared when a session reaches the post-handshake open
        # state so a stale failure message doesn't survive a
        # successful reconnect. Empty on a never-connected pairing
        # where the client task hasn't completed its first attempt.
        self._last_connect_error: str = ""
        # Per-job download state for ``download_artifacts``.
        # Populated by :meth:`download_artifacts` before the
        # request goes out, drained by the receive loop's
        # ``artifacts_start`` / ``artifacts_chunk`` /
        # ``artifacts_end`` dispatch, and force-completed in
        # :meth:`_run_session_loops`'s ``finally`` on session
        # loss (same shape as ``_submit_job_acks``).
        # ``DownloadArtifactsState`` holds the in-flight
        # :class:`BundleAssembler` plus the result future.
        self._artifacts_downloads: dict[str, _DownloadArtifactsState] = {}

    @property
    def receiver_hostname(self) -> str:
        return self._hostname

    @property
    def receiver_port(self) -> int:
        return self._port

    @property
    def pin_sha256(self) -> str:
        """OOB-verified pin (sha256 of the receiver's pubkey).

        Stable identifier for this client — matches the key in
        :attr:`OffloaderController._peer_link_clients` and the
        ``pin_sha256`` field on every event this client fires.
        Surfaced as a property so the controller's WS handler
        can confirm it matches the request before driving a
        :meth:`submit_job`.
        """
        return self._pin_sha256

    @property
    def is_session_open(self) -> bool:
        """True if a peer-link session is currently live (post-handshake, dispatch parked)."""
        return self._active_channel is not None

    @property
    def is_orphaned(self) -> bool:
        """True if the run loop has been poisoned and won't reconnect.

        Set in two cases, both of which mean reconnecting would
        just hammer the wrong endpoint:

        * Receiver-side ``terminate{reason: superseded}`` close
          — a newer offloader instance with the same
          ``dashboard_id`` has taken our slot. Reconnecting
          would collide with that instance.
        * Pin-mismatch on the post-handshake pin-check —
          ``session.remote_static_pub`` didn't match the
          OOB-confirmed pubkey, so we're talking to a
          rotated-but-legitimate receiver or to an attacker.
          Either way the operator's resolution (re-pair to
          confirm the new identity, or unpair) is the only
          path forward.

        The controller's restart path (a fresh :meth:`run`)
        clears the flag.
        """
        return self._orphaned

    @property
    def is_connecting(self) -> bool:
        """True if the run loop is alive but no session is currently open.

        The ``True`` window covers both the very first connect
        attempt (``_run_one_session`` before the post-handshake
        ``intent_response: ok``) and every subsequent reconnect
        cycle inside :meth:`run`'s backoff loop. Goes ``False``
        in two distinct directions:

        * Forward to ``connected``: a session reached the
          post-handshake open state and parked on the receive
          loop. :meth:`is_session_open` returns ``True``.
        * Sideways to ``orphaned``: a pin-mismatch / superseded
          close poisoned the run loop. :meth:`is_orphaned`
          returns ``True``.

        UI uses the tri-state to render "Connected" /
        "Connecting…" / "Disconnected (last error: …)"; an
        orphaned client is the disconnected case where the
        operator has to re-pair or unpair to recover.
        """
        return not self._orphaned and not self.is_session_open

    @property
    def last_connect_error(self) -> str:
        """Most-recent connection failure as a one-line description.

        Set by :meth:`_run_one_session`'s exception paths to
        ``f"{type(exc).__name__}: {exc}"`` for transport / Noise
        failures, to ``"auth rejected"`` for handshake-rejected
        sessions, and to ``"pin mismatch"`` for the orphan-on-
        rotation path. Cleared when a session reaches the
        post-handshake open state — a stale message must not
        survive a successful reconnect.

        Empty on a never-connected pairing (the run loop hasn't
        completed its first attempt yet) and on cleanly-stopped
        clients (``client_stopped`` close on controller
        shutdown).
        """
        return self._last_connect_error

    async def submit_job(
        self,
        *,
        job_id: str,
        configuration_filename: str,
        target: Literal["compile", "upload", "clean"],
        bundle_bytes: bytes,
        device_name: str = "",
        device_friendly_name: str = "",
    ) -> SubmitJobAckFrameData:
        """Send a ``submit_job`` header + chunked bundle and await the receiver's ack.

        Drives the offloader-side counterpart of the receiver's
        :class:`SubmitJobReceiver` accept path
        (:mod:`controllers.remote_build.submit_job`):

        1. Validate a session is live; raise
           :class:`PeerLinkNoSessionError` if not.
        2. Compute the bundle's SHA-256 + chunk count.
        3. Register a per-``job_id`` ack future on
           :attr:`_submit_job_acks` BEFORE the header goes out
           so a same-tick ack can't lose to the future
           registration (the receive loop runs on the same
           event loop; pre-registering avoids the race
           regardless).
        4. Send the header and stream every chunk through
           :meth:`PeerLinkChannel.send_frame`. A send failure
           (transport gone away mid-flow, JSON encode failure,
           Noise encrypt failure) raises
           :class:`SubmitJobSessionLostError` immediately
           rather than waiting for the timeout.
        5. Await the ack future with
           :data:`_SUBMIT_JOB_ACK_TIMEOUT_SECONDS`. Timeout
           raises :class:`SubmitJobTimeoutError`. Session loss
           during the wait raises
           :class:`SubmitJobSessionLostError` (the receive
           loop's ``finally`` propagates it via
           ``set_exception``).

        Concurrency: the WS dispatch is single-flight per
        connection, so the controller's WS handler invokes this
        sequentially per session. Multiple WS connections can
        invoke concurrently — distinct *job_id* values keep the
        ack futures separate, and :class:`PeerLinkChannel` holds
        the send lock that serialises wire encrypts. Same-
        ``job_id`` re-entry inside one session is rejected as
        :class:`PeerLinkNoSessionError` (a leftover ack future
        signals the previous flow hasn't completed); the WS
        layer should generate a fresh ``job_id`` per submit.

        No mid-session retry on timeout / session-loss: the
        receiver may have already accepted and queued the job,
        and a duplicate send under a fresh ``job_id`` would land
        a second :class:`FirmwareJob` on the receiver's queue.
        Operator-initiated retry on a fresh peer-link session
        is the correct recovery.
        """
        channel = self._require_open_channel(label="submit_job")
        ack_fut = self._register_submit_job_ack_future(job_id)
        try:
            await self._send_submit_job_frames(
                channel,
                job_id=job_id,
                configuration_filename=configuration_filename,
                target=target,
                bundle_bytes=bundle_bytes,
                device_name=device_name,
                device_friendly_name=device_friendly_name,
            )
            return await self._await_submit_job_ack(ack_fut, job_id=job_id)
        finally:
            self._submit_job_acks.pop(job_id, None)

    async def cancel_job(self, *, job_id: str) -> bool:
        """Send a ``cancel_job`` frame for *job_id* over the live session.

        Fire-and-forget — the receiver's :class:`JobFanout`
        will fan out the resulting ``JOB_CANCELLED`` event as a
        ``job_state_changed{status: cancelled}`` frame, which
        the offloader's existing
        :attr:`OFFLOADER_JOB_STATE_CHANGED` listener handles.
        No per-call ack future, no timeout state on
        :class:`PeerLinkClient` — the next ``job_state_changed``
        on the inbound stream is the confirmation. A cancel-
        of-already-terminal or unknown job is silently dropped
        at the receiver (debug-logged); the offloader UI shows
        the most recent ``status`` regardless.

        Returns ``True`` if the frame made it onto the wire,
        ``False`` on a same-tick channel failure (Noise encrypt
        / WS send returned ``False``). Raises
        :class:`PeerLinkNoSessionError` when no live session
        exists; the WS layer maps that to
        ``CommandError(PRECONDITION_FAILED)``.
        """
        channel = self._require_open_channel(label="cancel_job")
        frame: CancelJobFrameData = {"type": "cancel_job", "job_id": job_id}
        return await channel.send_frame(cast(dict[str, Any], frame))

    async def download_artifacts(self, *, job_id: str) -> DownloadArtifactsResult:
        """Fetch the build-artifact tarball for *job_id* from the paired receiver.

        Sends ``download_artifacts{job_id}``, parks on a per-
        job future the receive-loop dispatch fills as
        ``artifacts_start`` / ``artifacts_chunk`` /
        ``artifacts_end`` frames land. Returns a
        :class:`DownloadArtifactsResult` carrying the
        SHA-256-verified gzipped-tar bytes plus the
        receiver-resolved ``firmware.bin`` flash offset (taken
        from the ``artifacts_start`` header — the tarball
        itself doesn't carry the firmware partition's offset,
        only the ``extra`` flash-image entries do).

        Raises :class:`PeerLinkNoSessionError` if no live
        session exists; the WS layer maps that to
        ``CommandError(PRECONDITION_FAILED)``. Raises
        :class:`DownloadArtifactsError` (with structured
        ``reason``) on receiver-reported failure or
        offloader-side assembly mismatch. Raises
        :class:`SubmitJobSessionLostError` if the session
        ends mid-download (same drain shape as
        :meth:`submit_job`).

        No timeout — artifact tarballs are 1-2 MiB typical
        (max :data:`FIRMWARE_MAX_TOTAL_BYTES` = 16 MiB);
        chunk stream completes within seconds on a LAN. If a
        bound becomes necessary it slots in as
        ``asyncio.wait_for`` around the future.

        Same-``job_id`` re-entry inside one session raises
        :class:`PeerLinkNoSessionError` (a leftover future
        signals the previous download hasn't completed); the
        WS layer should serialise downloads or generate a
        fresh request per page-load.
        """
        channel = self._require_open_channel(label="download_artifacts")
        if job_id in self._artifacts_downloads:
            msg = (
                f"download_artifacts: future already registered for job_id={job_id!r} "
                f"(duplicate download on the same session)"
            )
            raise PeerLinkNoSessionError(msg)
        result: asyncio.Future[DownloadArtifactsResult] = asyncio.get_running_loop().create_future()
        self._artifacts_downloads[job_id] = _DownloadArtifactsState(future=result)
        try:
            frame: DownloadArtifactsFrameData = {
                "type": "download_artifacts",
                "job_id": job_id,
            }
            if not await channel.send_frame(cast(dict[str, Any], frame)):
                raise SubmitJobSessionLostError(
                    f"download_artifacts: request send failed mid-flow to "
                    f"{self._hostname}:{self._port}"
                )
            return await result
        finally:
            self._artifacts_downloads.pop(job_id, None)

    def _require_open_channel(self, *, label: str) -> PeerLinkChannel:
        """Return the live :class:`PeerLinkChannel` or raise :class:`PeerLinkNoSessionError`.

        ``label`` is folded into the exception message so each
        caller (``submit_job``, ``cancel_job``) names itself in
        the no-session log line. Every
        application-message sender that needs a live session
        flows through this single check; a future sender
        inherits the same exception class + WS-layer mapping
        without duplicating the channel-presence test.
        """
        channel = self._active_channel
        if channel is None:
            msg = f"{label}: no live peer-link session to {self._hostname}:{self._port}"
            raise PeerLinkNoSessionError(msg)
        return channel

    def _register_submit_job_ack_future(self, job_id: str) -> asyncio.Future[SubmitJobAckFrameData]:
        """Allocate + register the per-``job_id`` ack future, refusing duplicates.

        The future is registered on :attr:`_submit_job_acks`
        BEFORE the header goes out so a same-tick ack can't
        lose to the future registration (the receive loop runs
        on the same event loop; pre-registering avoids the
        race regardless). A second call for the same *job_id*
        while the first is still pending raises
        :class:`PeerLinkNoSessionError` — same exception class
        the WS layer maps to "refuse the submit, ask the caller
        to retry under a fresh id."
        """
        if job_id in self._submit_job_acks:
            msg = (
                f"submit_job: ack future already registered for job_id={job_id!r} "
                f"(duplicate submit on the same session)"
            )
            raise PeerLinkNoSessionError(msg)
        ack_fut: asyncio.Future[SubmitJobAckFrameData] = asyncio.get_running_loop().create_future()
        self._submit_job_acks[job_id] = ack_fut
        return ack_fut

    async def _send_submit_job_frames(
        self,
        channel: PeerLinkChannel,
        *,
        job_id: str,
        configuration_filename: str,
        target: Literal["compile", "upload", "clean"],
        bundle_bytes: bytes,
        device_name: str = "",
        device_friendly_name: str = "",
    ) -> None:
        """Send the ``submit_job`` header and every chunk frame, in order.

        Streams chunks via :func:`chunk_bundle`'s generator
        rather than materialising the list — slicing
        ``bundle_bytes`` produces a fresh ``bytes`` object per
        chunk, and holding them all alive at once would roughly
        double peak memory (up to :data:`BUNDLE_MAX_TOTAL_BYTES`,
        4 MiB). ``num_chunks`` is computed via integer ceil on
        ``total_bundle_bytes`` so the header still announces the
        exact count without a materialise step.

        Raises :class:`SubmitJobSessionLostError` immediately if
        any send returns ``False`` (transport gone away
        mid-flow, JSON encode failure, Noise encrypt failure)
        rather than ploughing on through the chunk loop and
        relying on the ack-await timeout to surface the failure.
        """
        total_bytes = len(bundle_bytes)
        num_chunks = (total_bytes + BUNDLE_CHUNK_SIZE_BYTES - 1) // BUNDLE_CHUNK_SIZE_BYTES
        header: SubmitJobFrameData = {
            "type": "submit_job",
            "job_id": job_id,
            "configuration_filename": configuration_filename,
            "target": target,
            "total_bundle_bytes": total_bytes,
            "num_chunks": num_chunks,
            "bundle_sha256": compute_bundle_sha256(bundle_bytes),
            "device_name": device_name,
            "device_friendly_name": device_friendly_name,
        }
        if not await channel.send_frame(cast(dict[str, Any], header)):
            raise SubmitJobSessionLostError(
                f"submit_job: header send failed mid-flow to {self._hostname}:{self._port}"
            )
        for chunk_index, raw, is_last in chunk_bundle(bundle_bytes):
            chunk_frame: SubmitJobChunkFrameData = {
                "type": "submit_job_chunk",
                "job_id": job_id,
                "chunk_index": chunk_index,
                "data_b64": encode_chunk(raw),
                "is_last": is_last,
            }
            if not await channel.send_frame(cast(dict[str, Any], chunk_frame)):
                raise SubmitJobSessionLostError(
                    f"submit_job: chunk {chunk_index} send failed mid-flow to "
                    f"{self._hostname}:{self._port}"
                )

    async def _await_submit_job_ack(
        self,
        ack_fut: asyncio.Future[SubmitJobAckFrameData],
        *,
        job_id: str,
    ) -> SubmitJobAckFrameData:
        """Park on *ack_fut* with a bounded timeout; raise structured errors.

        Timeout maps to :class:`SubmitJobTimeoutError`; session
        loss while parked surfaces as
        :class:`SubmitJobSessionLostError` (the receive loop's
        ``finally`` propagates it via ``set_exception``, which
        :meth:`asyncio.wait_for` re-raises).
        """
        try:
            return await asyncio.wait_for(ack_fut, timeout=_SUBMIT_JOB_ACK_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise SubmitJobTimeoutError(
                f"submit_job: no ack from {self._hostname}:{self._port} "
                f"after {_SUBMIT_JOB_ACK_TIMEOUT_SECONDS:.0f}s "
                f"(job_id={job_id!r})"
            ) from exc

    async def run(self) -> None:
        """Run the connect-loop forever. Cancellable.

        Each iteration:

        1. Open WS, drive Noise XX with ``intent="peer_link"``.
        2. On ``intent_response: ok``, fire
           ``OFFLOADER_PEER_LINK_OPENED``, park on the receive
           loop with a heartbeat task running alongside.
        3. On any session end (receiver-side ``terminate``,
           heartbeat miss, transport error, peer-hung-up),
           fire ``OFFLOADER_PEER_LINK_CLOSED`` with the
           appropriate reason.
        4. If the close reason is ``superseded``, mark the
           client orphaned and exit. Otherwise sleep
           exponential-backoff (interrupted on cancellation)
           and loop.

        Cancellation at any point sends a structured
        ``terminate{reason: client_stopped}`` if a session is
        active, then propagates the ``CancelledError`` to the
        controller so the task drops cleanly.
        """
        backoff = _RECONNECT_INITIAL_BACKOFF_SECONDS
        try:
            while not self._orphaned:
                close_reason = await self._run_one_session()
                # ``_last_connect_error`` was populated by the
                # exception paths inside ``_run_one_session`` (or
                # left empty for clean closes — receiver-driven
                # ``terminate`` frames, heartbeat timeouts that
                # reach here without an exception, etc.). Pass it
                # through so the close event carries the specific
                # failure detail alongside the category-level
                # ``reason``.
                self._fire_closed(close_reason, error_detail=self._last_connect_error)
                if close_reason == TerminateReason.SUPERSEDED.value:
                    _LOGGER.info(
                        "peer-link client to %s:%d superseded by another instance "
                        "with the same dashboard_id; orphaning",
                        self._hostname,
                        self._port,
                    )
                    self._orphaned = True
                    return
                if close_reason == _LOCAL_CLOSE_PIN_MISMATCH:
                    # Pin drift means we're either talking to a
                    # rotated-but-legitimate receiver or to an
                    # attacker; in both cases reconnecting just
                    # hammers the wrong endpoint. The bus event
                    # ``OFFLOADER_PAIR_PIN_MISMATCH`` already
                    # fired from ``_run_one_session`` carries the
                    # diagnostic payload, and the controller's
                    # listener has populated the alerts dict so
                    # the operator sees the warning. Resolution is
                    # user-driven: re-pair (clears the alert) or
                    # unpair (drops the row).
                    _LOGGER.warning(
                        "peer-link client to %s:%d observed pin drift; orphaning "
                        "until the operator re-pairs or unpairs",
                        self._hostname,
                        self._port,
                    )
                    self._orphaned = True
                    return
                # Reset backoff after a session that actually
                # reached ``intent_response: ok`` so a flaky path
                # doesn't permanently degrade to the cap. If we
                # never got past the handshake (transport error,
                # auth rejected, Noise failure), advance the
                # backoff exponentially — a broken receiver
                # mustn't be hammered every second.
                if self._session_was_opened:
                    backoff = _RECONNECT_INITIAL_BACKOFF_SECONDS
                else:
                    backoff = min(backoff * 2, _RECONNECT_MAX_BACKOFF_SECONDS)
                await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            # ``_run_one_session`` already sent the structured
            # ``terminate`` frame in its own CancelledError
            # handler (where the WS and Noise session are still
            # live as locals). All we need to do here is fire
            # the bus event so subscribers see the transition.
            # Even a cancellation before the first session
            # opened benefits from firing this — the controller
            # subscribed to ``OFFLOADER_PEER_LINK_CLOSED`` would
            # otherwise have to track "did this client ever
            # open" itself; the no-OPENED-then-CLOSED sequence
            # is a no-op for any subscriber that keys off
            # OPENED first.
            self._fire_closed(_LOCAL_CLOSE_CLIENT_STOPPED)
            raise

    async def _run_one_session(self) -> str:
        """Run one connect → handshake → receive loop iteration.

        Returns the close reason to propagate into
        ``OFFLOADER_PEER_LINK_CLOSED``. Always returns —
        exceptions are caught and mapped onto a local close
        reason. ``CancelledError`` is the one exception that
        propagates (the run loop's outer handler sends the
        terminate frame).
        """
        self._session_was_opened = False
        url = URL.build(scheme="ws", host=self._hostname, port=self._port, path=PEER_LINK_PATH)
        # ``total`` deliberately omitted: the peer-link session
        # is long-lived (idle-by-design once parked on the
        # receive loop), so a session-wide timeout would forcibly
        # drop a healthy session after ``_DEFAULT_TIMEOUT_SECONDS``.
        # Bound the *handshake* reads with ``asyncio.wait_for``
        # below — that's what the receiver does in
        # ``remote_build_peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS``
        # — so a stalled handshake still fails fast without
        # putting a ceiling on the dispatch loop's lifetime.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=_DEFAULT_TIMEOUT_SECONDS)
        try:
            async with (
                make_peer_link_http_session(timeout=timeout, resolver=self._resolver) as http,
                http.ws_connect(url, max_msg_size=APP_FRAME_MAX_BYTES) as ws,
            ):
                session = PeerLinkNoiseSession.initiator(self._identity_priv)
                msg3_payload = _json.dumps({"dashboard_id": self._dashboard_id})
                response_ct = await _drive_initiator_handshake_and_read_response(
                    ws=ws,
                    sess=session,
                    intent=PeerLinkIntent.PEER_LINK,
                    msg3_payload=msg3_payload,
                    read_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
                )
                # Pin-check the receiver's static pubkey BEFORE
                # decrypting / acting on the response. Noise XX
                # authenticates that the responder holds the
                # private key matching the pubkey it advertised,
                # so a mismatched pubkey here means we connected
                # to a different identity than the one we
                # OOB-confirmed at pair time. Could be a
                # legitimate receiver-side rotation or a MITM /
                # mDNS spoof; either way we abort before any
                # application frames flow.
                if session.remote_static_pub != self._pinned_static_x25519_pub:
                    self._fire_pin_mismatch(observed=session.remote_static_pub)
                    self._last_connect_error = "pin mismatch"
                    return _LOCAL_CLOSE_PIN_MISMATCH
                response = _json.loads(session.decrypt(response_ct))
                if (
                    not isinstance(response, dict)
                    or response.get("intent_response") != IntentResponse.OK.value
                ):
                    _LOGGER.warning(
                        "peer-link client to %s:%d rejected at handshake: %r",
                        self._hostname,
                        self._port,
                        response,
                    )
                    self._last_connect_error = "auth rejected"
                    return _LOCAL_CLOSE_AUTH_REJECTED
                # Lift the receiver's ``esphome_version`` off the
                # response so OPENED carries it onto the bus.
                receiver_version = _extract_receiver_esphome_version(response)
                # Session is live — build the shared channel
                # over (noise, ws), fire OPENED, park on the
                # receive loop with a heartbeat task running
                # alongside. Setting ``_session_was_opened``
                # tells :meth:`run`'s backoff logic to reset on
                # the next iteration. Clearing
                # ``_last_connect_error`` here means a successful
                # reconnect drops the previous failure message
                # off the operator-facing snapshot — a stale "the
                # last connect tried 4 attempts ago failed with
                # ConnectionRefusedError" would mislead the
                # operator into thinking the live session is
                # broken.
                channel = PeerLinkChannel(
                    noise=session, ws=ws, log_label=f"{self._hostname}:{self._port}"
                )
                self._session_was_opened = True
                self._last_connect_error = ""
                self._fire_opened(esphome_version=receiver_version)
                try:
                    return await self._run_session_loops(channel)
                except asyncio.CancelledError:
                    # Best-effort structured close before the
                    # WS goes away under us. The channel's
                    # ``send_terminate`` doesn't go through any
                    # ``_closing`` gate (this terminate IS the
                    # close), so the frame goes out reliably.
                    await channel.send_terminate(_LOCAL_CLOSE_CLIENT_STOPPED)
                    raise
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError, TypeError) as exc:
            _LOGGER.debug(
                "peer-link client to %s:%d transport error: %s",
                self._hostname,
                self._port,
                exc,
                exc_info=True,
            )
            self._last_connect_error = f"{type(exc).__name__}: {exc}"
            return _LOCAL_CLOSE_TRANSPORT_ERROR
        except NOISE_ERRORS as exc:
            _LOGGER.warning(
                "peer-link client to %s:%d Noise failure: %s",
                self._hostname,
                self._port,
                exc,
                exc_info=True,
            )
            self._last_connect_error = f"{type(exc).__name__}: {exc}"
            return _LOCAL_CLOSE_TRANSPORT_ERROR

    async def _run_session_loops(self, channel: PeerLinkChannel) -> str:
        """Run the receive loop with a heartbeat task in parallel.

        Returns the close reason. Both loops mutate a shared
        :class:`_SessionLoopState`: the receive loop bumps
        ``last_pong_at`` on each pong and writes
        ``close_reason`` on transport-error / terminate-frame
        / unknown-msg-type exits; the heartbeat task's
        ``_on_dead`` callback writes
        ``HEARTBEAT_TIMEOUT`` so the close reason reflects the
        real cause instead of falling through to the default
        ``peer_hung_up``. Both loops share the
        :class:`PeerLinkChannel` for encrypt / parse / send.
        """
        state = _SessionLoopState(
            last_pong_at=asyncio.get_running_loop().time(),
            close_reason=_LOCAL_CLOSE_PEER_HUNG_UP,
        )

        async def _send_ping(nonce: int) -> bool:
            return await channel.send_frame({"type": AppMessageType.PING.value, "nonce": nonce})

        async def _on_dead() -> None:
            state.close_reason = _LOCAL_CLOSE_HEARTBEAT_TIMEOUT
            _LOGGER.info(
                "peer-link client to %s:%d heartbeat timeout; closing",
                self._hostname,
                self._port,
            )
            # Best-effort close — include ``aiohttp.ClientError``
            # alongside the basic transport types because
            # :meth:`aiohttp.ClientWebSocketResponse.close` can
            # raise ``ClientConnectionError`` / ``ClientError``
            # when the peer has already gone away. Letting that
            # escape here would crash the heartbeat task and let
            # the receive loop fall through to its
            # ``peer_hung_up`` default, masking the real
            # heartbeat-timeout cause. ``CancelledError`` stays
            # unsuppressed (Python 3.8+ excludes it from
            # ``Exception``).
            with contextlib.suppress(OSError, RuntimeError, aiohttp.ClientError):
                await channel.ws.close()

        heartbeat_task = asyncio.create_task(
            run_peer_link_heartbeat(
                send_ping=_send_ping,
                last_pong_at=lambda: state.last_pong_at,
                on_dead=_on_dead,
            ),
            name=f"peer-link-client-heartbeat[{self._hostname}:{self._port}]",
        )
        # Expose the channel to :meth:`submit_job` for the
        # duration of the receive loop. Cleared in ``finally``
        # so a post-session :meth:`submit_job` raises
        # :class:`PeerLinkNoSessionError` instead of writing
        # into a stale channel.
        self._active_channel = channel
        # Bound the synchronous-dispatch lookup table once per
        # session — sync handlers fan an inbound frame into the
        # bus / ack futures with no need for the channel itself.
        # PING / PONG / TERMINATE / malformed each touch the
        # session loop's mutable state (close_reason,
        # last_pong_at) or the channel (PONG response), so they
        # stay branched in the loop body rather than fitting
        # the table's ``(self, parsed) -> None`` shape.
        sync_dispatch = self._build_sync_frame_dispatch()
        try:
            async for msg in channel.ws:
                parsed = channel.parse_frame(msg)
                if parsed is None:
                    # Any of the four malformed-frame branches —
                    # ``parse_frame`` already logged the per-branch
                    # context. Map to the offloader-side
                    # transport-error reason on the wire-status event.
                    state.close_reason = _LOCAL_CLOSE_TRANSPORT_ERROR
                    break
                msg_type = parsed.get("type")
                if msg_type == AppMessageType.PING.value:
                    nonce = parsed.get("nonce")
                    await channel.send_frame({"type": AppMessageType.PONG.value, "nonce": nonce})
                    continue
                if msg_type == AppMessageType.PONG.value:
                    state.last_pong_at = asyncio.get_running_loop().time()
                    continue
                if msg_type == AppMessageType.TERMINATE.value:
                    reason = parsed.get("reason")
                    state.close_reason = (
                        reason if isinstance(reason, str) else _LOCAL_CLOSE_PEER_HUNG_UP
                    )
                    break
                handler = sync_dispatch.get(msg_type) if isinstance(msg_type, str) else None
                if handler is not None:
                    handler(parsed)
                    continue
                _LOGGER.debug(
                    "peer-link client unknown app frame type %r from %s:%d; ignoring",
                    msg_type,
                    self._hostname,
                    self._port,
                )
            return state.close_reason
        finally:
            self._active_channel = None
            # Drain any in-flight :meth:`submit_job` callers so
            # they raise :class:`SubmitJobSessionLostError`
            # immediately instead of waiting on the per-flow
            # timeout. The session ended before the ack came
            # back; no point keeping the awaiter parked. Snapshot
            # the dict before iterating because
            # :meth:`submit_job`'s ``finally`` pops the entry as
            # soon as the future fires.
            for pending_job_id, pending_fut in list(self._submit_job_acks.items()):
                if not pending_fut.done():
                    pending_fut.set_exception(
                        SubmitJobSessionLostError(
                            f"submit_job: peer-link session to "
                            f"{self._hostname}:{self._port} ended before ack "
                            f"for job_id={pending_job_id!r}"
                        )
                    )
            # Same drain shape for 6a in-flight artifact downloads —
            # the receiver won't be sending any more chunks now
            # that the session's gone; resolve every pending
            # future so :meth:`download_artifacts` unwinds.
            for pending_job_id, dl_state in list(self._artifacts_downloads.items()):
                if not dl_state.future.done():
                    dl_state.future.set_exception(
                        SubmitJobSessionLostError(
                            f"download_artifacts: peer-link session to "
                            f"{self._hostname}:{self._port} ended before "
                            f"artifacts_end for job_id={pending_job_id!r}"
                        )
                    )
            heartbeat_task.cancel()
            # Drain via ``gather(return_exceptions=True)`` rather
            # than ``suppress(CancelledError) + await`` — suppressing
            # CancelledError swallows any outer cancellation that
            # arrives during the drain and breaks the propagation
            # contract (see ``feedback_no_suppress_cancelled_error``).
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    def _build_sync_frame_dispatch(
        self,
    ) -> dict[str, Callable[[dict[str, Any]], None]]:
        """Return the inbound-frame → sync handler map for one session.

        Built once per session (in :meth:`_run_session_loops`)
        rather than per inbound frame to keep the receive-loop
        hot path's per-frame work down to one dict lookup. The
        bound-method values capture ``self`` so adding a new
        sync frame type is a one-line table entry plus the
        handler implementation, no loop-body branch.

        Excluded from the table on purpose:
        ``PING`` / ``PONG`` / ``TERMINATE`` mutate the
        session-local :class:`_SessionLoopState` or close the
        loop, neither of which fits the ``(parsed)`` shape.
        Malformed frames (``parse_frame`` returned ``None``) are
        a separate branch upstream of this lookup.
        """
        return {
            AppMessageType.QUEUE_STATUS.value: self._dispatch_queue_status,
            AppMessageType.SUBMIT_JOB_ACK.value: self._dispatch_submit_job_ack,
            AppMessageType.JOB_STATE_CHANGED.value: self._dispatch_job_state_changed,
            AppMessageType.JOB_OUTPUT.value: self._dispatch_job_output,
            AppMessageType.ARTIFACTS_START.value: self._dispatch_artifacts_start,
            AppMessageType.ARTIFACTS_CHUNK.value: self._dispatch_artifacts_chunk,
            AppMessageType.ARTIFACTS_END.value: self._dispatch_artifacts_end,
        }

    def _dispatch_queue_status(self, parsed: dict[str, Any]) -> None:
        """Validate a ``queue_status`` frame and fire the offloader-side bus event.

        Drop silently on shape mismatch — the receiver will
        broadcast another snapshot on the next queue
        transition. The frame's ``queue_depth`` is ``int``;
        :func:`frame_schema` wraps every ``int`` field with
        :func:`not_bool` so a ``bool`` (which subclasses
        ``int``) doesn't slip through as a valid integer.
        """
        if not is_valid_frame(_QUEUE_STATUS_SCHEMA, parsed):
            self._log_malformed("queue_status", parsed)
            return
        self._fire_queue_status(parsed["idle"], parsed["running"], parsed["queue_depth"])

    def _dispatch_submit_job_ack(self, parsed: dict[str, Any]) -> None:
        """Resolve the matching ack future for an inbound ``submit_job_ack`` frame.

        Drops silently on:

        * Shape mismatch (missing / wrong-typed required fields)
          — the awaiter times out cleanly rather than seeing a
          malformed frame as a successful accept.
        * No matching future under *job_id* — the awaiter
          already raised :class:`SubmitJobTimeoutError` and
          popped its entry, or the receiver acked a job we
          didn't submit.
        * Future already done — duplicate ack under one
          *job_id*; the first wins and the second's
          ``set_result`` would raise ``InvalidStateError``.

        Optional ``reason`` (only present on rejection) is read
        post-validate and copied through.
        """
        if not is_valid_frame(_SUBMIT_JOB_ACK_SCHEMA, parsed):
            self._log_malformed("submit_job_ack", parsed)
            return
        job_id = cast(str, parsed["job_id"])
        ack_fut = self._submit_job_acks.get(job_id)
        if ack_fut is None or ack_fut.done():
            _LOGGER.debug(
                "peer-link client dropping submit_job_ack from %s:%d "
                "(job_id=%r, has_future=%s, done=%s)",
                self._hostname,
                self._port,
                job_id,
                ack_fut is not None,
                ack_fut.done() if ack_fut is not None else False,
            )
            return
        accepted = cast(bool, parsed["accepted"])
        ack: SubmitJobAckFrameData = {
            "type": "submit_job_ack",
            "job_id": job_id,
            "accepted": accepted,
        }
        # ``SubmitJobAckFrameData.reason`` is ``NotRequired`` and
        # carries the rejection code on ``accepted=False``. A
        # receiver that includes ``reason`` on accept is off-
        # contract — preserve the typed shape by dropping the
        # spurious field (logged at debug for the operator).
        reason = parsed.get("reason")
        if isinstance(reason, str):
            if accepted:
                _LOGGER.debug(
                    "peer-link client dropping spurious reason=%r on accepted ack "
                    "from %s:%d (job_id=%r)",
                    reason,
                    self._hostname,
                    self._port,
                    job_id,
                )
            else:
                ack["reason"] = reason
        ack_fut.set_result(ack)

    def _log_malformed(self, frame_type: str, parsed: dict[str, Any]) -> None:
        """Debug-log a frame that failed shape validation.

        Single call site for the per-dispatcher
        "malformed X frame from Y:Z" line so the format string
        doesn't drift across the four dispatchers.
        """
        _LOGGER.debug(
            "peer-link client malformed %s frame from %s:%d: %r",
            frame_type,
            self._hostname,
            self._port,
            parsed,
        )

    def _dispatch_job_state_changed(self, parsed: dict[str, Any]) -> None:
        """Validate + fan an inbound ``job_state_changed`` frame onto the bus.

        Same pattern as :meth:`_dispatch_queue_status`: validate
        first, drop silently on shape mismatch (a future
        retransmit will land cleanly), enrich with this
        client's receiver coordinates so subscribers can
        disambiguate transitions across multiple paired
        receivers.
        """
        if not is_valid_frame(_JOB_STATE_CHANGED_SCHEMA, parsed):
            self._log_malformed("job_state_changed", parsed)
            return
        if cast(str, parsed["status"]) not in _JOB_STATE_CHANGED_VALID_STATUS:
            self._log_malformed("job_state_changed", parsed)
            return
        wire = cast(JobStateChangedFrameData, parsed)
        payload: OffloaderJobStateChangedData = {
            "receiver_hostname": self._hostname,
            "receiver_port": self._port,
            "pin_sha256": self._pin_sha256,
            "job_id": wire["job_id"],
            "status": wire["status"],
            "error_message": wire["error_message"],
        }
        self._bus.fire(EventType.OFFLOADER_JOB_STATE_CHANGED, payload)

    def _dispatch_job_output(self, parsed: dict[str, Any]) -> None:
        """Validate + fan an inbound ``job_output`` frame onto the bus.

        High-rate path during an active build (one frame per
        line of compiler / linker output). Validate cheaply and
        drop on shape mismatch; subscribers see ``stream`` /
        ``line`` typed by :class:`OffloaderJobOutputData`.
        """
        if not is_valid_frame(_JOB_OUTPUT_SCHEMA, parsed):
            self._log_malformed("job_output", parsed)
            return
        if cast(str, parsed["stream"]) not in _JOB_OUTPUT_VALID_STREAM:
            self._log_malformed("job_output", parsed)
            return
        wire = cast(JobOutputFrameData, parsed)
        payload: OffloaderJobOutputData = {
            "receiver_hostname": self._hostname,
            "receiver_port": self._port,
            "pin_sha256": self._pin_sha256,
            "job_id": wire["job_id"],
            "stream": wire["stream"],
            "line": wire["line"],
        }
        self._bus.fire(EventType.OFFLOADER_JOB_OUTPUT, payload)

    def _dispatch_artifacts_start(self, parsed: dict[str, Any]) -> None:
        """Validate ``artifacts_start`` + install the assembler for the in-flight download.

        Drops silently on shape mismatch / unknown job_id —
        the receive loop is hot and a malformed frame from a
        buggy peer shouldn't crash anyone. A stray
        ``artifacts_start`` for a job we never asked for
        means the awaiter already raised + popped its state
        (or it was a different session entirely); the safe
        thing is to ignore.
        """
        if not is_valid_frame(_ARTIFACTS_START_SCHEMA, parsed):
            self._log_malformed("artifacts_start", parsed)
            return
        wire = cast(ArtifactsStartFrameData, parsed)
        state = self._artifacts_downloads.get(wire["job_id"])
        if state is None:
            self._log_malformed("artifacts_start", parsed)
            return
        try:
            state.assembler = BundleAssembler(
                total_bytes=wire["total_bytes"],
                num_chunks=wire["num_chunks"],
                sha256_hex=wire["artifacts_sha256"],
                max_total_bytes=FIRMWARE_MAX_TOTAL_BYTES,
            )
        except BundleAssemblerError as exc:
            if not state.future.done():
                state.future.set_exception(
                    DownloadArtifactsError(
                        f"download_artifacts: invalid start header: {exc}",
                        reason="invalid_start_header",
                    )
                )
            return
        state.firmware_offset = wire["firmware_offset"]

    def _dispatch_artifacts_chunk(self, parsed: dict[str, Any]) -> None:
        """Validate ``artifacts_chunk`` + feed the assembler.

        Out-of-order / oversized / decode-failure chunks
        from a buggy receiver resolve the future with
        :class:`DownloadArtifactsError`; the awaiter unwinds
        and the WS layer surfaces the structured reason.
        """
        if not is_valid_frame(_ARTIFACTS_CHUNK_SCHEMA, parsed):
            self._log_malformed("artifacts_chunk", parsed)
            return
        wire = cast(ArtifactsChunkFrameData, parsed)
        state = self._artifacts_downloads.get(wire["job_id"])
        if state is None or state.assembler is None:
            self._log_malformed("artifacts_chunk", parsed)
            return
        try:
            raw = decode_chunk(wire["data_b64"])
            state.assembler.feed(wire["chunk_index"], raw, is_last=wire["is_last"])
        except BundleAssemblerError as exc:
            if not state.future.done():
                state.future.set_exception(
                    DownloadArtifactsError(
                        f"download_artifacts: chunk failed: {exc}",
                        reason=exc.code.value,
                    )
                )

    def _dispatch_artifacts_end(self, parsed: dict[str, Any]) -> None:
        """Validate ``artifacts_end`` + resolve the download future.

        Success path (``accepted=true``): finalise the
        assembler (validates count + SHA-256), set the
        future to the bytes. Failure path
        (``accepted=false``): pop ``reason`` and set the
        future to a :class:`DownloadArtifactsError` carrying
        it.
        """
        if not is_valid_frame(_ARTIFACTS_END_SCHEMA, parsed):
            self._log_malformed("artifacts_end", parsed)
            return
        wire = cast(ArtifactsEndFrameData, parsed)
        state = self._artifacts_downloads.get(wire["job_id"])
        if state is None or state.future.done():
            return
        if not wire["accepted"]:
            reason = parsed.get("reason", "unknown")
            state.future.set_exception(
                DownloadArtifactsError(
                    f"download_artifacts: receiver rejected ({reason})",
                    reason=str(reason),
                )
            )
            return
        if state.assembler is None:
            state.future.set_exception(
                DownloadArtifactsError(
                    "download_artifacts: receiver acked success without sending artifacts_start",
                    reason="missing_start",
                )
            )
            return
        try:
            tarball = state.assembler.finalise()
        except BundleAssemblerError as exc:
            state.future.set_exception(
                DownloadArtifactsError(
                    f"download_artifacts: finalise failed: {exc}",
                    reason=exc.code.value,
                )
            )
            return
        state.future.set_result(
            DownloadArtifactsResult(tarball=tarball, firmware_offset=state.firmware_offset)
        )

    def _fire_opened(self, *, esphome_version: str = "") -> None:
        payload: OffloaderPeerLinkOpenedData = {
            "receiver_hostname": self._hostname,
            "receiver_port": self._port,
            "pin_sha256": self._pin_sha256,
            "esphome_version": esphome_version,
        }
        self._bus.fire(EventType.OFFLOADER_PEER_LINK_OPENED, payload)

    def _fire_closed(self, reason: str, *, error_detail: str = "") -> None:
        payload: OffloaderPeerLinkClosedData = {
            "receiver_hostname": self._hostname,
            "receiver_port": self._port,
            "pin_sha256": self._pin_sha256,
            "reason": reason,
            "error_detail": error_detail,
        }
        self._bus.fire(EventType.OFFLOADER_PEER_LINK_CLOSED, payload)

    def _fire_pin_mismatch(self, *, observed: bytes) -> None:
        """Fire ``OFFLOADER_PAIR_PIN_MISMATCH`` after a peer-link pin drift.

        Same event shape the pair-status listener already fires
        from :meth:`OffloaderController._apply_pair_status_result`
        on its own pin-drift branch. The controller listens for
        the event and stores the alert in
        ``_offloader_alerts`` so the snapshot path
        (``subscribe_events.initial_state.offloader_alerts``)
        carries it for late-subscribing tabs.

        ``expected_pin`` / ``observed_pin`` are the
        SHA-256 hashes of the pinned + observed pubkeys, in the
        same lowercase-hex form
        :class:`StoredPairing.pin_sha256` uses on disk.
        """
        payload: OffloaderPairPinMismatchData = {
            "receiver_hostname": self._hostname,
            "receiver_port": self._port,
            "receiver_label": self._receiver_label,
            "pin_sha256": self._pin_sha256,
            "expected_pin": pin_sha256_for_pubkey(self._pinned_static_x25519_pub),
            "observed_pin": pin_sha256_for_pubkey(observed),
        }
        self._bus.fire(EventType.OFFLOADER_PAIR_PIN_MISMATCH, payload)

    def _fire_queue_status(self, idle: bool, running: bool, queue_depth: int) -> None:
        """Fire ``OFFLOADER_QUEUE_STATUS_CHANGED`` for an inbound snapshot.

        The peer-link receive loop validates the wire shape
        (boolean / int) before getting here, so the event
        payload's primitive contract holds without re-checking.
        Listeners on the bus include the
        :class:`OffloaderController`'s cache update and the
        ``subscribe_events`` re-broadcast.
        """
        payload: OffloaderQueueStatusChangedData = {
            "receiver_hostname": self._hostname,
            "receiver_port": self._port,
            "pin_sha256": self._pin_sha256,
            "idle": idle,
            "running": running,
            "queue_depth": queue_depth,
        }
        self._bus.fire(EventType.OFFLOADER_QUEUE_STATUS_CHANGED, payload)
