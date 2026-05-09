"""
Peer-link Noise WS handler for the remote-build feature (issue #106).

Phase 4a-r1 part 4. Owns the wire shape of the
``/remote-build/peer-link`` WebSocket endpoint: drives the
``Noise_XX_25519_ChaChaPoly_SHA256`` handshake, parses the
offloader's ``intent`` discriminator out of the cleartext msg1
payload + the encrypted msg3 payload, dispatches to the
controller's helper methods (`record_pair_request` /
`lookup_peer_for_session` / `lookup_peer_for_status`), and wraps
the response in a ChaCha20-Poly1305 transport frame.

Handshake-payload confidentiality (per the Noise XX wire spec
that ``helpers.peer_link_noise`` documents):

* msg1 (offloader → receiver, plaintext): ``{"intent": "..."}``.
  Coarse discriminator only; sensitive fields wait until msg3.
* msg2 (receiver → offloader, encrypted with the freshly-mixed
  ``ee`` + ``es`` chain): empty payload. The encryption + the
  carried responder static key are what the offloader pins
  against in the ``preview`` flow.
* msg3 (offloader → receiver, encrypted with the now-finalized
  cipher): ``{"dashboard_id": "...", "label": "..."}`` for
  pair_request; ``{"dashboard_id": "..."}`` for peer_link /
  pair_status; empty for preview.

After the handshake completes, the receiver sends one
post-handshake transport frame carrying
``{"intent_response": "..."}`` and (for now) closes the WS. Phase
5+ extends the ``intent="peer_link"`` happy path to keep the WS
open for application messages (bundle upload, build trigger,
firmware download); part 4 just lays the dispatch foundation.

Timeouts: handshake reads have an explicit timeout so a peer that
opens a TCP connection and never sends the first frame can't pin
a coroutine forever. The timeout is generous (10s) because the
Noise XX handshake itself is local-DH cheap; only the network
round-trip costs anything, and that's bounded by LAN latency.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from ..helpers import json as _json
from ..helpers.peer_link_identity import get_or_create_peer_link_identity
from ..helpers.peer_link_noise import (
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from ..models import IntentResponse

if TYPE_CHECKING:
    from .remote_build import RemoteBuildController

_LOGGER = logging.getLogger(__name__)

PEER_LINK_PATH = "/remote-build/peer-link"

# Coarse intent discriminators the offloader sends in the cleartext
# msg1 payload. Anything else returns ``intent_response="rejected"``
# and closes.
_VALID_INTENTS = frozenset({"preview", "pair_request", "peer_link", "pair_status"})

# Generous handshake timeout. Noise XX is three messages with one
# DH each; latency is bounded by the LAN round-trip. 10s tolerates
# a slow / loaded receiver; a peer that hasn't sent msg1 in 10s
# isn't a real offloader.
_HANDSHAKE_READ_TIMEOUT_SECONDS = 10.0


def make_peer_link_handler(
    controller: RemoteBuildController,
) -> Callable[[web.Request], Awaitable[web.WebSocketResponse]]:
    """
    Build the aiohttp handler for ``/remote-build/peer-link``.

    Closure captures the controller so the handler can call into
    ``record_pair_request`` / ``lookup_peer_for_*`` without each
    invocation re-resolving the singleton through the request's
    app instance.
    """

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        peer_ip = request.remote or ""
        try:
            await _drive_peer_link_session(controller, ws, peer_ip)
        except Exception:
            _LOGGER.exception("peer-link session error from %s", peer_ip)
        finally:
            if not ws.closed:
                await ws.close()
        return ws

    return handler


async def _drive_peer_link_session(  # noqa: PLR0911 — the early-returns are the handshake's natural failure cliffs
    controller: RemoteBuildController,
    ws: web.WebSocketResponse,
    peer_ip: str,
) -> None:
    """
    Drive one peer-link Noise session from handshake to response.

    Split out of the handler so tests can exercise the dispatch
    against a fake ``WebSocketResponse`` without standing up an
    aiohttp server.
    """
    loop = asyncio.get_running_loop()
    identity = await loop.run_in_executor(
        None, get_or_create_peer_link_identity, controller._db.settings.config_dir
    )
    session = PeerLinkNoiseSession.responder(identity.private_bytes)

    # --- handshake msg1 (offloader → receiver, plaintext payload) ---
    msg1_payload = await _read_handshake_message(session, ws, "msg1")
    if msg1_payload is None:
        return
    intent = _parse_intent(msg1_payload)
    if intent not in _VALID_INTENTS:
        # Complete the handshake before rejecting so the offloader
        # can see the rejection in an authenticated frame rather
        # than as a raw transport close. Send empty msg2, expect
        # msg3, then send the rejection.
        if not await _send_handshake_message(session, ws, b"", "msg2"):
            return
        if await _read_handshake_message(session, ws, "msg3") is None:
            return
        await _send_response(session, ws, IntentResponse.REJECTED)
        return

    # --- handshake msg2 (receiver → offloader, empty encrypted) ---
    if not await _send_handshake_message(session, ws, b"", "msg2"):
        return

    # --- handshake msg3 (offloader → receiver, encrypted payload) ---
    msg3_payload = await _read_handshake_message(session, ws, "msg3")
    if msg3_payload is None:
        return
    msg3 = _parse_json(msg3_payload) or {}

    try:
        remote_static_pub = session.remote_static_pub
    except HandshakeNotCompleteError:
        _LOGGER.warning(
            "peer-link handshake from %s did not yield remote static pubkey",
            peer_ip,
        )
        return
    pin = pin_sha256_for_pubkey(remote_static_pub)
    dashboard_id = _str_or_empty(msg3.get("dashboard_id"))
    label = _str_or_empty(msg3.get("label"))

    response = await _dispatch_intent(
        controller=controller,
        intent=intent,
        dashboard_id=dashboard_id,
        label=label,
        pin_sha256=pin,
        static_x25519_pub=remote_static_pub,
        peer_ip=peer_ip,
    )
    await _send_response(session, ws, response)


async def _dispatch_intent(
    *,
    controller: RemoteBuildController,
    intent: str,
    dashboard_id: str,
    label: str,
    pin_sha256: str,
    static_x25519_pub: bytes,
    peer_ip: str,
) -> IntentResponse:
    """
    Resolve a single peer-link intent into a typed :class:`IntentResponse`.

    Pure dispatch logic, callable directly from tests so the
    intent → controller-call routing is verified without the WS /
    Noise plumbing in the loop. See :class:`IntentResponse` for the
    per-intent response semantics.
    """
    if intent == "preview":
        # Preview captures the responder's static pubkey via the
        # handshake transcript; nothing else to do server-side.
        return IntentResponse.OK
    if intent == "pair_request":
        if not controller.is_pairing_window_open():
            return IntentResponse.NO_PAIRING_WINDOW
        return await controller.record_pair_request(
            dashboard_id=dashboard_id,
            pin_sha256=pin_sha256,
            static_x25519_pub=static_x25519_pub,
            label=label,
            peer_ip=peer_ip,
        )
    if intent == "peer_link":
        return await controller.lookup_peer_for_session(
            dashboard_id=dashboard_id, pin_sha256=pin_sha256
        )
    if intent == "pair_status":
        return await controller.lookup_peer_for_status(
            dashboard_id=dashboard_id, pin_sha256=pin_sha256
        )
    return IntentResponse.REJECTED


# ---------------------------------------------------------------------------
# WS / Noise plumbing helpers
# ---------------------------------------------------------------------------


async def _read_handshake_message(
    session: PeerLinkNoiseSession,
    ws: web.WebSocketResponse,
    label: str,
) -> bytes | None:
    """Read one binary WS frame as a Noise handshake message; return payload or None on error."""
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=_HANDSHAKE_READ_TIMEOUT_SECONDS)
    except TimeoutError:
        _LOGGER.debug("peer-link timed out waiting for %s", label)
        return None
    if msg.type != WSMsgType.BINARY:
        _LOGGER.debug(
            "peer-link expected binary frame for %s; got %s",
            label,
            msg.type,
        )
        return None
    try:
        return session.read_handshake_message(msg.data)
    except Exception:
        _LOGGER.warning("peer-link Noise %s read failed", label, exc_info=True)
        return None


async def _send_handshake_message(
    session: PeerLinkNoiseSession,
    ws: web.WebSocketResponse,
    payload: bytes,
    label: str,
) -> bool:
    """Send one Noise handshake message as a binary WS frame; return True on success."""
    try:
        encoded = session.write_handshake_message(payload)
    except Exception:
        _LOGGER.warning("peer-link Noise %s write failed", label, exc_info=True)
        return False
    try:
        await ws.send_bytes(encoded)
    except (ConnectionResetError, asyncio.CancelledError):
        raise
    except Exception:
        _LOGGER.debug("peer-link send %s failed", label, exc_info=True)
        return False
    return True


async def _send_response(
    session: PeerLinkNoiseSession,
    ws: web.WebSocketResponse,
    response: IntentResponse,
) -> None:
    """Send the post-handshake intent_response as a single ChaCha20-Poly1305 frame."""
    body = _json.dumps({"intent_response": response.value})
    try:
        encrypted = session.encrypt(body)
    except Exception:
        _LOGGER.warning("peer-link transport encrypt failed", exc_info=True)
        return
    try:
        await ws.send_bytes(encrypted)
    except (ConnectionResetError, asyncio.CancelledError):
        raise
    except Exception:
        _LOGGER.debug("peer-link send response failed", exc_info=True)


def _parse_intent(payload: bytes) -> str:
    """Pull the ``intent`` field out of the cleartext msg1 payload, defaulting to empty."""
    parsed = _parse_json(payload)
    if not isinstance(parsed, dict):
        return ""
    return _str_or_empty(parsed.get("intent"))


def _parse_json(payload: bytes) -> Any | None:
    """Decode a JSON payload, returning ``None`` on any decode failure."""
    if not payload:
        return None
    try:
        return _json.loads(payload)
    except _json.JSONDecodeError:
        return None


def _str_or_empty(value: object) -> str:
    """Return the string value or empty when not a string."""
    return value if isinstance(value, str) else ""
