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
from ....models import IntentResponse, PeerLinkIntent, RejectReason
from ..provision import receiver_supports_auto_provision

if TYPE_CHECKING:
    from .handshake import _HandshakeStep

_LOGGER = logging.getLogger(__name__)

# Generous handshake timeout. Noise XX is three messages with one
# DH each; latency is bounded by the LAN round-trip. 10s tolerates
# a slow / loaded receiver; a peer that hasn't sent msg1 in 10s
# isn't a real offloader.
_HANDSHAKE_READ_TIMEOUT_SECONDS = 10.0

# Cap msg3's offloader-supplied ``label`` before it lands on disk
# + on the event bus. Truncation (not rejection) — a too-long label
# is cosmetic noise, not a reason to fail pairing.
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

    Any send-side failure (peer hung up, WS-state error, OS-level
    socket error) is debug-logged and surfaces as ``False`` so the
    caller can short-circuit the rest of the handshake / response
    sequence. Disconnects are normal operation on flaky LANs.
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
    *,
    reason: RejectReason | None,
    requires_pairing_key: bool = False,
) -> None:
    """Send the post-handshake intent_response as a single ChaCha20-Poly1305 frame.

    Payload carries the response discriminator plus the receiver's
    ``esphome_version`` and ``auto_provision_supported`` capability on
    every intent. The long-lived ``peer_link`` session captures both
    onto the :class:`StoredPairing` so a receiver upgrade surfaces in
    pick_build_path's version-compat gate on the next session-open
    without operator action.

    *reason* rides along on non-OK responses (additive field;
    older offloaders ignore it) so the offloader can tell a
    terminal rejection apart from a transient one.

    *requires_pairing_key* is set only on the ``preview`` response
    (never on ``pair_request``, so a refusal stays indistinguishable
    from a closed window): it tells the offloader this is a key-mode
    server so the UI can require the bootstrap key up front.
    """
    payload: dict[str, str | bool] = {
        "intent_response": response.value,
        "esphome_version": esphome_version,
        "auto_provision_supported": receiver_supports_auto_provision(),
    }
    if reason is not None:
        payload["reason"] = reason.value
    if requires_pairing_key:
        payload["requires_pairing_key"] = True
    body = _json.dumps(payload)
    try:
        encrypted = session.encrypt(body)
    except NOISE_ERRORS:
        _LOGGER.warning("peer-link transport encrypt failed", exc_info=True)
        return
    await _send_bytes_safely(ws, encrypted, log_label="response")


def _parse_intent(payload: bytes) -> PeerLinkIntent | None:
    """
    Pull the ``intent`` field out of the cleartext msg1 payload.

    Returns the parsed :class:`PeerLinkIntent` or ``None`` on any
    malformed branch (missing field, non-string, unknown value,
    bad JSON). Caller maps ``None`` to
    ``IntentResponse.REJECTED`` and closes the WS *after*
    completing the handshake so the rejection arrives in an
    authenticated transport frame.
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

    Strips whitespace and truncates at
    :data:`_PEER_LABEL_MAX_CHARS`; non-string / missing values
    fall through to ``""``. Bounding matters because the value
    lands on disk and on the event bus.
    """
    raw = _str_or_empty(value).strip()
    return raw[:_PEER_LABEL_MAX_CHARS]
