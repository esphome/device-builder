"""
Peer-link low-level WS / Noise plumbing helpers.

Handshake-message read / write, intent-response send, JSON /
intent parsing, and label normalisation. Pure leaf helpers
consumed by the handshake driver, the channel, and the session
loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web
from esphome.const import __version__ as esphome_version

from ....helpers import json as _json
from ....helpers.peer_link_noise import NOISE_ERRORS, PeerLinkNoiseSession
from ....models import IntentResponse, PeerLinkIntent

if TYPE_CHECKING:
    from . import _HandshakeStep

_LOGGER = logging.getLogger(__name__)

# Generous handshake timeout. Noise XX is three messages with one
# DH each; latency is bounded by the LAN round-trip. 10s tolerates
# a slow / loaded receiver; a peer that hasn't sent msg1 in 10s
# isn't a real offloader.
_HANDSHAKE_READ_TIMEOUT_SECONDS = 10.0

# Cap msg3's offloader-supplied ``label`` before it lands in
# settings + the event payload. Peer-supplied input over the wire
# could be arbitrarily large within the WS frame limit; truncation
# (rather than rejection) matches the "two-side flow, usually one
# user" framing — a too-long label is cosmetic noise, not a reason
# to fail pairing. 128 chars matches the cap the legacy token-label
# path uses (``_TOKEN_LABEL_MAX`` in :mod:`controllers.remote_build`).
_PEER_LABEL_MAX_CHARS = 128


async def _read_handshake_message(
    session: PeerLinkNoiseSession,
    ws: web.WebSocketResponse,
    step: _HandshakeStep,
) -> bytes | None:
    """Read one binary WS frame as a Noise handshake message; return payload or None on error."""
    try:
        msg = await asyncio.wait_for(ws.receive(), timeout=_HANDSHAKE_READ_TIMEOUT_SECONDS)
    except TimeoutError:
        _LOGGER.debug("peer-link timed out waiting for %s", step)
        return None
    if msg.type != WSMsgType.BINARY:
        _LOGGER.debug(
            "peer-link expected binary frame for %s; got %s",
            step,
            msg.type,
        )
        return None
    try:
        return session.read_handshake_message(msg.data)
    except NOISE_ERRORS:
        _LOGGER.warning("peer-link Noise %s read failed", step, exc_info=True)
        return None


async def _send_bytes_safely(
    ws: web.WebSocketResponse,
    encoded: bytes,
    *,
    log_label: str,
) -> bool:
    """
    Write *encoded* to *ws* and return True on success.

    Any send-side failure — peer hung up
    (``ConnectionResetError``), aiohttp/WS-state error, OS-level
    socket error — is debug-logged and surfaces as a False
    return so the caller can short-circuit the rest of the
    handshake / response sequence. Disconnects are normal-
    operation events on flaky LANs; ``api/ws.py`` similarly
    treats ``ConnectionResetError`` on send as not worth a
    traceback.
    """
    try:
        await ws.send_bytes(encoded)
    except Exception:
        _LOGGER.debug("peer-link send %s failed", log_label, exc_info=True)
        return False
    return True


async def _send_handshake_message(
    session: PeerLinkNoiseSession,
    ws: web.WebSocketResponse,
    payload: bytes,
    step: _HandshakeStep,
) -> bool:
    """Send one Noise handshake message as a binary WS frame; return True on success."""
    try:
        encoded = session.write_handshake_message(payload)
    except NOISE_ERRORS:
        _LOGGER.warning("peer-link Noise %s write failed", step, exc_info=True)
        return False
    return await _send_bytes_safely(ws, encoded, log_label=str(step))


async def _send_response(
    session: PeerLinkNoiseSession,
    ws: web.WebSocketResponse,
    response: IntentResponse,
) -> None:
    """Send the post-handshake intent_response as a single ChaCha20-Poly1305 frame.

    The payload carries the response discriminator
    (``intent_response``) plus the receiver's
    :data:`esphome.const.__version__` (``esphome_version``).
    Both halves run the same shared field on every intent so a
    caller that opens any flow — preview / pair_request /
    pair_status / peer_link — gets the receiver's version
    alongside the discriminator. Offloader-side consumption
    centres on the long-lived ``peer_link`` session, where the
    captured value lands on :attr:`StoredPairing.esphome_version`
    and refreshes on every reconnect so a receiver upgrade
    surfaces in pick_build_path's version-compat gate on the
    next session-open without operator action.
    """
    body = _json.dumps({"intent_response": response.value, "esphome_version": esphome_version})
    try:
        encrypted = session.encrypt(body)
    except NOISE_ERRORS:
        _LOGGER.warning("peer-link transport encrypt failed", exc_info=True)
        return
    await _send_bytes_safely(ws, encrypted, log_label="response")


def _parse_intent(payload: bytes) -> PeerLinkIntent | None:
    """
    Pull the ``intent`` field out of the cleartext msg1 payload.

    Returns the parsed :class:`PeerLinkIntent` member or ``None``
    when the payload doesn't carry a recognised intent (missing
    field, non-string, unknown wire value, malformed JSON). The
    caller maps ``None`` to ``IntentResponse.REJECTED`` and
    closes the WS after completing the handshake (so the
    rejection arrives in an authenticated transport frame).
    """
    parsed = _parse_json(payload)
    if not isinstance(parsed, dict):
        return None
    raw = parsed.get("intent")
    if not isinstance(raw, str):
        return None
    try:
        return PeerLinkIntent(raw)
    except ValueError:
        return None


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


def _normalize_label(value: object) -> str:
    """
    Normalise an msg3-supplied ``label`` to a stripped, length-bounded form.

    Peer-supplied input lands on disk + on the event bus; an
    unbounded label would let a misbehaving offloader push
    multi-megabyte strings into ``.device-builder.json`` and
    every receiver-UI subscriber. Strip whitespace and truncate
    at :data:`_PEER_LABEL_MAX_CHARS`; non-string / missing
    values fall through to ``""`` so the receiver UI just shows
    no label rather than failing the pairing.
    """
    raw = _str_or_empty(value).strip()
    return raw[:_PEER_LABEL_MAX_CHARS]
