"""
Offloader-side one-shot peer-link Noise WS round-trip helpers (issue #106).

Initiator counterpart of
:mod:`controllers.remote_build.peer_link`'s responder. Opens a
``ws://<receiver>:<peer_link_port>/remote-build/peer-link``
WebSocket, drives the three Noise XX handshake messages from the
offloader side, optionally exchanges application-level
``intent`` / ``intent_response`` framing, and surfaces the
captured receiver static pubkey hash to the caller.

This module is the wire-shape twin of
``peer_link.py``: same handshake, opposite role.
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
the intent + msg3 payload + accepted-response set. The
long-lived ``peer_link`` intent is driven by
:class:`.client.PeerLinkClient` and reuses
:func:`_drive_initiator_handshake_and_read_response` for the
shared msg1/msg2/msg3 + response read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from yarl import URL

from ....helpers import json as _json
from ....helpers.peer_link_noise import (
    NOISE_ERRORS,
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from ....helpers.peer_link_resolver import make_peer_link_http_session
from ....models import (
    PAIRING_VERSION_MAX_LEN,
    IntentResponse,
    PeerLinkIntent,
)
from .._client_models import (
    InitiatorRoundTrip,
    PairStatusResult,
    PeerLinkClientError,
    RequestPairResult,
)
from ..peer_link import PEER_LINK_PATH

if TYPE_CHECKING:
    from aiohttp.resolver import AbstractResolver


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
# ``peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS``);
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
    :func:`controllers.remote_build.peer_link._drive_peer_link_session`:

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
