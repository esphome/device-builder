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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from ..helpers import json as _json
from ..helpers.dashboard_identity import DASHBOARD_ID_MAX_CHARS, DASHBOARD_ID_PATTERN
from ..helpers.peer_link_identity import get_or_create_peer_link_identity
from ..helpers.peer_link_noise import (
    NOISE_ERRORS,
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from ..models import IntentResponse, PeerLinkIntent


class _HandshakeStep(StrEnum):
    """
    The three Noise XX handshake messages, in order.

    Used as a label-typed argument to ``_read_handshake_message``
    / ``_send_handshake_message`` so log lines and timeout-error
    messages identify the specific step. Members are the wire-
    convention short names from the Noise spec (``e`` for the
    initiator's ephemeral on msg1, ``e, ee, s, es`` for msg2's
    composite, ``s, se`` for msg3) but we name them ``MSG1`` /
    ``MSG2`` / ``MSG3`` for grep-readability against any
    debugger / log output.
    """

    MSG1 = "msg1"
    MSG2 = "msg2"
    MSG3 = "msg3"


@dataclass(frozen=True)
class _DispatchInput:
    """
    Per-session inputs to :func:`_dispatch_intent`.

    Bundles the six values ``_drive_peer_link_session`` extracts
    from the Noise handshake transcript + msg3 payload + WS
    request: the intent discriminator, the offloader-supplied
    metadata (dashboard_id, label), the handshake-derived
    identity (pin_sha256 + static_x25519_pub) and the connection
    metadata (peer_ip). Frozen because the dispatcher only reads;
    a single object beats threading six kwargs through the call
    site.
    """

    intent: PeerLinkIntent
    dashboard_id: str
    label: str
    pin_sha256: str
    static_x25519_pub: bytes
    peer_ip: str


if TYPE_CHECKING:
    from .remote_build import RemoteBuildController

_LOGGER = logging.getLogger(__name__)

PEER_LINK_PATH = "/remote-build/peer-link"

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


async def make_peer_link_handler(
    controller: RemoteBuildController,
    config_dir: Path,
) -> Callable[[web.Request], Awaitable[web.WebSocketResponse]]:
    """
    Build the aiohttp handler for ``/remote-build/peer-link``.

    Loads the X25519 peer-link identity once at handler-factory
    time and captures it in the closure so each incoming WS
    connection constructs its ``PeerLinkNoiseSession`` from
    already-loaded bytes instead of hitting disk + an executor
    hop on every handshake. Identity is stable for the process
    lifetime; rotation tears down + rebuilds the runner, which
    re-enters this factory.

    ``config_dir`` is passed in explicitly rather than read off
    the controller's private ``_db`` chain — the caller
    (``DeviceBuilder._build_and_start_remote_build_runner``)
    already has it in hand, and a sibling module reaching
    through ``controller._db.settings.config_dir`` would be
    a single-leading-underscore boundary violation.
    """
    loop = asyncio.get_running_loop()
    identity = await loop.run_in_executor(None, get_or_create_peer_link_identity, config_dir)
    identity_priv = identity.private_bytes

    async def handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        peer_ip = request.remote or ""
        try:
            await _drive_peer_link_session(controller, ws, peer_ip, identity_priv)
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
    identity_priv: bytes,
) -> None:
    """
    Drive one peer-link Noise session from handshake to response.

    Split out of the handler so tests can exercise the dispatch
    against a fake ``WebSocketResponse`` without standing up an
    aiohttp server.
    """
    session = PeerLinkNoiseSession.responder(identity_priv)

    # --- handshake msg1 (offloader → receiver, plaintext payload) ---
    msg1_payload = await _read_handshake_message(session, ws, _HandshakeStep.MSG1)
    if msg1_payload is None:
        return
    intent = _parse_intent(msg1_payload)
    if intent is None:
        # Complete the handshake before rejecting so the offloader
        # can see the rejection in an authenticated frame rather
        # than as a raw transport close. Send empty msg2, expect
        # msg3, then send the rejection.
        if not await _send_handshake_message(session, ws, b"", _HandshakeStep.MSG2):
            return
        if await _read_handshake_message(session, ws, _HandshakeStep.MSG3) is None:
            return
        await _send_response(session, ws, IntentResponse.REJECTED)
        return

    # --- handshake msg2 (receiver → offloader, empty encrypted) ---
    if not await _send_handshake_message(session, ws, b"", _HandshakeStep.MSG2):
        return

    # --- handshake msg3 (offloader → receiver, encrypted payload) ---
    msg3_payload = await _read_handshake_message(session, ws, _HandshakeStep.MSG3)
    if msg3_payload is None:
        return
    parsed = _parse_json(msg3_payload)
    msg3 = parsed if isinstance(parsed, dict) else {}

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
    label = _normalize_label(msg3.get("label"))

    response = await _dispatch_intent(
        controller,
        _DispatchInput(
            intent=intent,
            dashboard_id=dashboard_id,
            label=label,
            pin_sha256=pin,
            static_x25519_pub=remote_static_pub,
            peer_ip=peer_ip,
        ),
    )
    await _send_response(session, ws, response)


async def _dispatch_intent(
    controller: RemoteBuildController,
    inp: _DispatchInput,
) -> IntentResponse:
    """
    Resolve a single peer-link intent into a typed :class:`IntentResponse`.

    Pure dispatch logic, callable directly from tests so the
    intent → controller-call routing is verified without the WS /
    Noise plumbing in the loop. See :class:`IntentResponse` for the
    per-intent response semantics. The caller (the WS driver) has
    already validated the wire string into a :class:`PeerLinkIntent`
    member; an unknown wire value returns ``IntentResponse.REJECTED``
    before reaching this function.
    """
    if inp.intent is PeerLinkIntent.PREVIEW:
        # Preview captures the responder's static pubkey via the
        # handshake transcript; nothing else to do server-side
        # and the offloader doesn't need a dashboard_id yet.
        return IntentResponse.OK

    # Every other intent identifies the offloader by dashboard_id;
    # an empty / missing / malformed value would create or look up
    # nonsense rows, so reject before any controller call. The
    # alphabet + length contract is the same one
    # ``RemoteBuildController._validate_dashboard_id`` uses for the
    # WS-command path; both consumers import the constants from
    # ``helpers.dashboard_identity`` so they can't drift.
    if (
        not inp.dashboard_id
        or len(inp.dashboard_id) > DASHBOARD_ID_MAX_CHARS
        or not DASHBOARD_ID_PATTERN.fullmatch(inp.dashboard_id)
    ):
        return IntentResponse.REJECTED

    if inp.intent is PeerLinkIntent.PAIR_REQUEST:
        if not controller.is_pairing_window_open():
            return IntentResponse.NO_PAIRING_WINDOW
        return await controller.record_pair_request(
            dashboard_id=inp.dashboard_id,
            pin_sha256=inp.pin_sha256,
            static_x25519_pub=inp.static_x25519_pub,
            label=inp.label,
            peer_ip=inp.peer_ip,
        )
    if inp.intent is PeerLinkIntent.PEER_LINK:
        return await controller.lookup_peer_for_session(
            dashboard_id=inp.dashboard_id, pin_sha256=inp.pin_sha256
        )
    # PeerLinkIntent.PAIR_STATUS — exhaustive enum match.
    return await controller.lookup_peer_for_status(
        dashboard_id=inp.dashboard_id, pin_sha256=inp.pin_sha256
    )


# ---------------------------------------------------------------------------
# WS / Noise plumbing helpers
# ---------------------------------------------------------------------------


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
    """Send the post-handshake intent_response as a single ChaCha20-Poly1305 frame."""
    body = _json.dumps({"intent_response": response.value})
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
