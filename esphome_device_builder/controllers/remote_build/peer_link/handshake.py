"""
Peer-link Noise XX handshake driver + intent dispatch.

Reads the three-step XX handshake off the WS, parses the
offloader's ``intent`` discriminator out of msg1 (plaintext) +
msg3 (encrypted payload), and routes to the controller's
record / lookup helpers. Hands off to the long-lived session
loop for a successful ``peer_link`` intent; one-shot for
``preview`` / ``pair_request`` / ``pair_status``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from aiohttp import web

from ....helpers.dashboard_identity import DASHBOARD_ID_MAX_CHARS, DASHBOARD_ID_PATTERN
from ....helpers.peer_link_noise import (
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from ....models import IntentResponse, PeerLinkIntent
from .session import _run_peer_link_session
from .wire_io import (
    _normalize_label,
    _parse_intent,
    _parse_json,
    _read_handshake_message,
    _send_handshake_message,
    _send_response,
    _str_or_empty,
)

if TYPE_CHECKING:
    from ..receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)


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


async def _drive_peer_link_session(  # noqa: PLR0911 — the early-returns are the handshake's natural failure cliffs
    controller: ReceiverController,
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
    _LOGGER.info("peer-link WS accepted from %s", peer_ip)
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
    _LOGGER.info(
        "peer-link handshake from %s ok (intent=%s dashboard_id=%s observed_offloader_pin=%s)",
        peer_ip,
        intent.value,
        dashboard_id,
        pin,
    )

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

    # Hand off to the long-lived application session for
    # ``intent="peer_link"`` on a successful auth. Every other
    # intent — including a ``REJECTED`` peer_link — closes the WS
    # via the handler's ``finally`` (the legacy one-shot shape).
    if intent is PeerLinkIntent.PEER_LINK and response is IntentResponse.OK:
        await _run_peer_link_session(
            controller=controller,
            ws=ws,
            session=session,
            dashboard_id=dashboard_id,
            peer_ip=peer_ip,
        )


async def _dispatch_intent(
    controller: ReceiverController,
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
    # :func:`controllers.remote_build._validators.validate_dashboard_id`
    # enforces on the WS-command path; both consumers import the
    # constants from ``helpers.dashboard_identity`` so they can't
    # drift.
    if (
        not inp.dashboard_id
        or len(inp.dashboard_id) > DASHBOARD_ID_MAX_CHARS
        or not DASHBOARD_ID_PATTERN.fullmatch(inp.dashboard_id)
    ):
        return IntentResponse.REJECTED

    if inp.intent is PeerLinkIntent.PAIR_REQUEST:
        # The pairing-window gate lives inside ``record_pair_request``
        # rather than here so it can short-circuit only the cases
        # where new admin authorization is actually being requested
        # (new PENDING row created, or pubkey rotated under an
        # existing PENDING / APPROVED row). A re-pair against an
        # already-APPROVED row whose pubkey still matches doesn't
        # need admin action and bypasses the window check — the
        # offloader is just re-establishing existing trust.
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
