"""
Offloader-side one-shot peer-link Noise WS round-trip helpers.

Initiator counterpart of :mod:`controllers.remote_build.peer_link`'s
responder. :func:`drive_initiator_round_trip` owns the shared
TCP-connect + 3-message Noise XX handshake + post-handshake
response-read flow; each public ``preview_pair`` / ``request_pair`` /
``await_pair_status`` is a thin wrapper that supplies the intent
discriminator and the encrypted msg3 payload. The long-lived
``peer_link`` intent driven by :class:`.client.PeerLinkClient`
reuses :func:`_drive_initiator_handshake_and_read_response` for
the same msg1/msg2/msg3 + response read.
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
from ....helpers.version_compat import is_pep440_version
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

# Built once at module level instead of inlined as
# ``(*NOISE_ERRORS, _json.JSONDecodeError)`` in the ``except``
# clause so mypy can verify the type without tripping on its
# star-unpack-in-except limitation.
_RESPONSE_DECODE_ERRORS: tuple[type[Exception], ...] = (
    *NOISE_ERRORS,
    _json.JSONDecodeError,
)


# 10s matches the receiver-side per-step timeout in
# ``peer_link.wire_io._HANDSHAKE_READ_TIMEOUT_SECONDS`` so we don't
# give up before the receiver does, but doesn't pin a coroutine forever.
_DEFAULT_TIMEOUT_SECONDS = 10.0


# 1h budget for the long-poll: well above the receiver's 5-min
# default pairing-window lifetime so a typical admin "walk away
# to verify, come back, click Accept" flow doesn't trip the
# offloader's aiohttp timeout. Real cancellation paths
# (controller stop, unpair) cancel the listener task directly.
_PAIR_STATUS_TIMEOUT_SECONDS = 3600.0


# 64 KiB cap on inbound control-plane frames vs aiohttp's 4 MiB
# default — keeps a malicious / buggy receiver from spending
# ~4 MiB of memory + Noise-decrypt + JSON-parse CPU per
# round-trip. Does NOT apply to the firmware ``peer_link``
# intent: that uses a separate streaming driver scoped to one
# Noise frame (~64 KiB).
_CONTROL_RESPONSE_MAX_BYTES = 64 * 1024


def _extract_receiver_esphome_version(response: dict[str, Any]) -> str:
    """
    Lift ``esphome_version`` off the post-handshake response.

    Returns ``""`` when missing, non-str, oversize
    (:data:`PAIRING_VERSION_MAX_LEN`, the disk-side cap mirrored
    here so the wire and storage seams can't drift apart), or not a
    valid PEP 440 version — so a malformed / injected string never
    reaches storage or a later ``pip install`` argument.
    """
    value = response.get("esphome_version", "")
    if not isinstance(value, str) or len(value) > PAIRING_VERSION_MAX_LEN:
        return ""
    if not is_pep440_version(value):
        return ""
    return value


def _extract_auto_provision_supported(response: dict[str, Any]) -> bool:
    """
    Lift the receiver's ``auto_provision_supported`` flag off the response.

    Missing or non-bool ⇒ ``False`` — an older receiver that never sends
    the field is treated as unable to provision.
    """
    value = response.get("auto_provision_supported", False)
    return value if isinstance(value, bool) else False


def _build_ws_url(hostname: str, port: int) -> URL:
    """
    Build the peer-link WS URL for *hostname* / *port*.

    :class:`yarl.URL` auto-brackets IPv6 literals and raises
    ``ValueError`` on path-injection-shaped hosts (slash, ``?``,
    ``#``, ``@``, embedded ``:port``). Scheme is ``ws://`` —
    Noise XX provides transport security on plain TCP.
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
    """
    Drive Noise XX msg1/msg2/msg3 + read the post-handshake response ciphertext.

    Shared by :func:`drive_initiator_round_trip` and
    :meth:`PeerLinkClient._run_one_session`. Pre: *ws* is
    connected; *sess* is a fresh initiator. Post: *sess* is in
    transport mode.
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
    """
    Run one Noise XX round-trip from the initiator side.

    Sends msg1 with cleartext ``{"intent": "..."}``, exchanges
    msg2/msg3, then reads + decrypts the receiver's
    ``{"intent_response": "..."}`` transport frame. Raises
    :class:`PeerLinkClientError` on any transport / handshake /
    decode failure with the underlying exception attached as
    ``__cause__``; callers branch on
    :attr:`InitiatorRoundTrip.intent_response` per intent.
    """
    sess = PeerLinkNoiseSession.initiator(identity_priv)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    label = f"peer-link {intent.value} to {hostname}:{port}"

    # ``_build_ws_url`` is inside the try as defense-in-depth:
    # the WS-command boundary's ``_validate_hostname`` already
    # rejects path-injection-shaped hosts as ``INVALID_ARGS``,
    # but a caller that bypasses the validator would otherwise
    # see ``ValueError`` escape as ``INTERNAL_ERROR`` instead
    # of ``UNAVAILABLE``.
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
    """
    Run an ``intent="preview"`` round-trip; return the receiver's pin_sha256.

    The returned pin renders for OOB verification against the
    receiver's "Build server" Settings card before the offloader
    calls ``request_pair``.
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
    """
    Run an ``intent="pair_request"`` round-trip; return the receiver's response.

    Caller is responsible for the TOCTOU pin check: compare
    :attr:`RequestPairResult.pin_sha256` against the value the
    user OOB-confirmed in ``preview_pair`` BEFORE persisting any
    state. Unknown ``intent_response`` strings raise
    :class:`PeerLinkClientError`.
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
    """
    Run an ``intent="pair_status"`` long-poll round-trip.

    Receiver holds the response open until the admin clicks
    Accept/Reject or the pairing window closes; client-side
    budget is :data:`_PAIR_STATUS_TIMEOUT_SECONDS` (1h, well
    above the receiver's 5-min default window). Caller is
    responsible for the pin-drift check against
    :attr:`StoredPairing.pin_sha256` — a mismatch is a
    peer-revoked signal, not a silent rotation. Unknown
    ``intent_response`` strings raise :class:`PeerLinkClientError`.
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
