"""
Offloader-side peer-link Noise WS client (issue #106 phase 4a-o part 2).

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
``pair_request``, ``pair_status``, eventually ``peer_link``);
only the msg3 payload and which response codes count as success
differ. :func:`drive_initiator_round_trip` owns the shared flow;
each public ``preview_pair`` / ``request_pair`` / ``await_pair_status``
function (parts 2-4 of phase 4a-o) is a thin wrapper that
provides the intent + msg3 payload + accepted-response set.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from yarl import URL

from ..helpers import json as _json
from ..helpers.peer_link_noise import (
    NOISE_ERRORS,
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from ..models import (
    EventType,
    IntentResponse,
    OffloaderPairPinMismatchData,
    OffloaderPeerLinkClosedData,
    OffloaderPeerLinkOpenedData,
    OffloaderQueueStatusChangedData,
    PeerLinkIntent,
)
from .remote_build_peer_link import (
    APP_FRAME_MAX_BYTES,
    PEER_LINK_PATH,
    AppMessageType,
    PeerLinkChannel,
    TerminateReason,
    run_peer_link_heartbeat,
)

if TYPE_CHECKING:
    from ..helpers.event_bus import EventBus

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
# This cap explicitly does NOT apply to the future firmware-bytes
# ``peer_link`` intent (issue #106 phase 4c onward). That payload
# is megabytes of compiled firmware and will use a separate
# streaming driver — Noise has a hard 65535-byte ciphertext frame
# limit, so the firmware path will read many small frames and
# stream them to disk, not a single ``receive_bytes()`` call.
# When that driver lands, it gets its own ``max_msg_size``
# tuned to one Noise frame (~64 KiB + slack); this constant
# stays scoped to the JSON status responses.
_CONTROL_RESPONSE_MAX_BYTES = 64 * 1024


class PeerLinkClientError(RuntimeError):
    """Raised on transport / handshake / decrypt failure on the offloader side.

    Wraps the underlying ``aiohttp.ClientError`` /
    :class:`OSError` / :class:`asyncio.TimeoutError` /
    :data:`NOISE_ERRORS` chain into one type the WS-command
    layer can map to a single ``UNAVAILABLE`` :class:`CommandError`
    without having to enumerate every transport failure mode.
    """


@dataclass(frozen=True)
class InitiatorRoundTrip:
    """One offloader-side Noise XX round-trip's outputs.

    Returned by :func:`drive_initiator_round_trip`. Bundles
    everything a caller might need from a completed handshake +
    response: the receiver's pubkey (so :func:`preview_pair`
    can hash it), the ``intent_response`` value (so callers can
    branch on PENDING / APPROVED / REJECTED / NO_PAIRING_WINDOW),
    and the full decoded response dict for any future fields a
    caller wants beyond the discriminator.
    """

    intent_response: str
    remote_static_pub: bytes
    response: dict[str, Any]


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
            aiohttp.ClientSession(timeout=timeout) as http,
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
    offloader call ``request_pair`` (phase 4a-o part 3).
    """
    rt = await drive_initiator_round_trip(
        hostname=hostname,
        port=port,
        identity_priv=identity_priv,
        intent=PeerLinkIntent.PREVIEW,
    )
    if rt.intent_response != IntentResponse.OK.value:
        msg = f"peer-link preview rejected with intent_response={rt.intent_response!r}"
        raise PeerLinkClientError(msg)
    return pin_sha256_for_pubkey(rt.remote_static_pub)


@dataclass(frozen=True)
class RequestPairResult:
    """Outcome of an ``intent="pair_request"`` round-trip from the offloader.

    Returned by :func:`request_pair` after the Noise XX
    handshake completes and the receiver's
    ``intent_response`` has been received.

    * :attr:`status` carries the receiver's response verbatim
      (``IntentResponse.PENDING`` for a freshly-created /
      refreshed pending row, ``IntentResponse.APPROVED`` for a
      re-pair against a pre-existing approved row,
      ``IntentResponse.REJECTED`` for receiver-side decline /
      pin mismatch, ``IntentResponse.NO_PAIRING_WINDOW`` for
      a closed window).
    * :attr:`pin_sha256` is the lowercase-hex hash of the
      receiver's static X25519 pubkey actually observed on the
      live handshake. The caller compares this against the
      pin the user OOB-confirmed in ``preview_pair``; a
      mismatch indicates the receiver rotated identity (or an
      active MITM intervened) between preview and request.
    * :attr:`remote_static_pub` is the raw 32-byte pubkey
      itself, for storage in :class:`StoredPairing`.
    """

    status: IntentResponse
    pin_sha256: str
    remote_static_pub: bytes


async def request_pair(
    *,
    hostname: str,
    port: int,
    identity_priv: bytes,
    label: str,
    dashboard_id: str,
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


@dataclass(frozen=True)
class PairStatusResult:
    """Outcome of an ``intent="pair_status"`` long-poll round-trip.

    Returned by :func:`await_pair_status` after the Noise XX
    handshake completes and the receiver's ``intent_response``
    has been received. The receiver-side handler parks
    indefinitely on the bus event channel until either an admin
    click flips the row or the pairing window closes (firing
    removed events that wake the wait), so the round-trip can
    legitimately take seconds to many minutes; the caller's
    listener task is the only thing that puts an upper bound
    on how long the WS stays open (via cancellation on
    ``unpair`` / controller stop).

    * :attr:`status` is the receiver's verbatim response —
      :attr:`IntentResponse.APPROVED` if the matching
      ``StoredPeer`` row is APPROVED, or
      :attr:`IntentResponse.REJECTED` if no row matches (admin
      clicked Reject, or window-close cleared the receiver's
      pending dict, or pin drift on the receiver side). The
      caller flips local state accordingly.
      :attr:`IntentResponse.PENDING` doesn't appear on this
      path — the receiver doesn't return PENDING from
      ``intent="pair_status"``; the long-poll keeps waiting.
    * :attr:`pin_sha256` is the lowercase-hex hash of the
      receiver's static X25519 pubkey observed on the live
      handshake. The :class:`StoredPairing` consumer compares
      this against its stored ``pin_sha256`` so a receiver-side
      identity rotation between :func:`request_pair` and the
      first :func:`await_pair_status` doesn't silently slide a
      compromised pubkey into ``APPROVED`` state.
    """

    status: IntentResponse
    pin_sha256: str


async def await_pair_status(
    *,
    hostname: str,
    port: int,
    identity_priv: bytes,
    dashboard_id: str,
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
# Phase 5a-2 — Long-lived offloader-side peer-link session.
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


@dataclass
class _SessionLoopState:
    """Mutable state shared between the session's receive loop and heartbeat task.

    Held by :meth:`PeerLinkClient._run_session_loops` and read /
    written by both the receive loop and the heartbeat
    callback so close-cause information flows in either
    direction.

    The receive loop bumps :attr:`last_pong_at` on each pong;
    the heartbeat task reads it through a ``lambda`` to decide
    whether to fire ``on_dead``. The receive loop and the
    heartbeat task each write :attr:`close_reason` on the
    branches they own — receive loop on transport-error /
    terminate-from-peer / unknown-msg-type, heartbeat on
    timeout — so the final close reason reflects the real
    cause rather than falling back to ``peer_hung_up`` (the
    "WS exited iteration without anyone setting a reason"
    default).

    Lifting this out of the receive loop's locals into a small
    object avoids the ``nonlocal`` pattern that would otherwise
    have to be threaded through the heartbeat closure.
    """

    last_pong_at: float
    close_reason: str


class PeerLinkClient:
    """
    Long-lived offloader-side peer-link Noise WS session.

    One instance per APPROVED :class:`StoredPairing`, owned by
    :class:`RemoteBuildController` (5a-2 wiring). Drive via
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
        receiver_label: str,
        bus: EventBus,
    ) -> None:
        self._hostname = receiver_hostname
        self._port = receiver_port
        self._identity_priv = identity_priv
        self._dashboard_id = dashboard_id
        # Pinned receiver pubkey from the OOB-verified pair flow,
        # captured during ``preview_pair`` and stored on
        # :class:`StoredPairing.static_x25519_pub`. Compared
        # against ``session.remote_static_pub`` post-handshake on
        # every connect so an attacker with their own X25519
        # keypair can't complete Noise XX against this client and
        # reach the application channel. ``receiver_label`` is
        # carried so the pin-mismatch alert can name the row at
        # firing time.
        self._pinned_static_x25519_pub = pinned_static_x25519_pub
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

    @property
    def receiver_hostname(self) -> str:
        return self._hostname

    @property
    def receiver_port(self) -> int:
        return self._port

    @property
    def is_orphaned(self) -> bool:
        """True if the run loop has been poisoned and won't reconnect.

        Set in two cases, both of which mean reconnecting would
        just hammer the wrong endpoint:

        * Receiver-side ``terminate{reason: superseded}`` close
          — a newer offloader instance with the same
          ``dashboard_id`` has taken our slot. Reconnecting
          would collide with that instance.
        * Pin-mismatch on the post-handshake pin-check (4a-o
          part 5) — ``session.remote_static_pub`` didn't match
          the OOB-confirmed pubkey, so we're talking to a
          rotated-but-legitimate receiver or to an attacker.
          Either way the operator's resolution (re-pair to
          confirm the new identity, or unpair) is the only
          path forward.

        The controller's restart path (a fresh :meth:`run`)
        clears the flag.
        """
        return self._orphaned

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
                self._fire_closed(close_reason)
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
                aiohttp.ClientSession(timeout=timeout) as http,
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
                    return _LOCAL_CLOSE_AUTH_REJECTED
                # Session is live — build the shared channel
                # over (noise, ws), fire OPENED, park on the
                # receive loop with a heartbeat task running
                # alongside. Setting ``_session_was_opened``
                # tells :meth:`run`'s backoff logic to reset on
                # the next iteration.
                channel = PeerLinkChannel(
                    noise=session, ws=ws, log_label=f"{self._hostname}:{self._port}"
                )
                self._session_was_opened = True
                self._fire_opened()
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
            return _LOCAL_CLOSE_TRANSPORT_ERROR
        except NOISE_ERRORS as exc:
            _LOGGER.warning(
                "peer-link client to %s:%d Noise failure: %s",
                self._hostname,
                self._port,
                exc,
                exc_info=True,
            )
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
                if msg_type == AppMessageType.QUEUE_STATUS.value:
                    # Receiver pushed a fresh firmware-queue
                    # snapshot. Validate the three fields are
                    # the expected primitive shapes before
                    # firing — a malformed frame from a buggy
                    # peer shouldn't land an ``OffloaderQueueStatusChangedData``
                    # with a ``None`` ``queue_depth`` on the bus
                    # for downstream consumers (the cache, the
                    # ``subscribe_events`` re-broadcast). Drop
                    # silently on shape mismatch — the receiver
                    # will broadcast another snapshot on the
                    # next queue transition.
                    idle = parsed.get("idle")
                    running = parsed.get("running")
                    queue_depth = parsed.get("queue_depth")
                    if (
                        not isinstance(idle, bool)
                        or not isinstance(running, bool)
                        or not isinstance(queue_depth, int)
                        or isinstance(queue_depth, bool)
                    ):
                        _LOGGER.debug(
                            "peer-link client malformed queue_status frame from %s:%d: %r",
                            self._hostname,
                            self._port,
                            parsed,
                        )
                        continue
                    self._fire_queue_status(idle, running, queue_depth)
                    continue
                _LOGGER.debug(
                    "peer-link client unknown app frame type %r from %s:%d; ignoring",
                    msg_type,
                    self._hostname,
                    self._port,
                )
            return state.close_reason
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task

    def _fire_opened(self) -> None:
        payload: OffloaderPeerLinkOpenedData = {
            "receiver_hostname": self._hostname,
            "receiver_port": self._port,
        }
        self._bus.fire(EventType.OFFLOADER_PEER_LINK_OPENED, payload)

    def _fire_closed(self, reason: str) -> None:
        payload: OffloaderPeerLinkClosedData = {
            "receiver_hostname": self._hostname,
            "receiver_port": self._port,
            "reason": reason,
        }
        self._bus.fire(EventType.OFFLOADER_PEER_LINK_CLOSED, payload)

    def _fire_pin_mismatch(self, *, observed: bytes) -> None:
        """Fire ``OFFLOADER_PAIR_PIN_MISMATCH`` after a peer-link pin drift.

        Same event shape the pair-status listener already fires
        from :meth:`RemoteBuildController._apply_pair_status_result`
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
            "expected_pin": pin_sha256_for_pubkey(self._pinned_static_x25519_pub),
            "observed_pin": pin_sha256_for_pubkey(observed),
        }
        self._bus.fire(EventType.OFFLOADER_PAIR_PIN_MISMATCH, payload)

    def _fire_queue_status(self, idle: bool, running: bool, queue_depth: int) -> None:
        """Fire ``OFFLOADER_QUEUE_STATUS_CHANGED`` for an inbound snapshot.

        The peer-link receive loop validates the wire shape
        (boolean / int) before getting here, so the event
        payload's primitive contract holds without re-checking.
        Listeners on the bus include the offloader-side
        ``RemoteBuildController`` cache update and the
        ``subscribe_events`` re-broadcast.
        """
        payload: OffloaderQueueStatusChangedData = {
            "receiver_hostname": self._hostname,
            "receiver_port": self._port,
            "idle": idle,
            "running": running,
            "queue_depth": queue_depth,
        }
        self._bus.fire(EventType.OFFLOADER_QUEUE_STATUS_CHANGED, payload)
