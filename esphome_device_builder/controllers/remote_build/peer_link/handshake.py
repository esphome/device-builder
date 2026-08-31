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
from ....helpers.ip import port_or_zero
from ....helpers.peer_link_noise import (
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from ....models import IntentResponse, PeerLinkIntent, RejectReason
from .._intent import IntentOutcome
from ..display_identity import dashboard_display_identity
from .session import _run_peer_link_session
from .wire_io import (
    _bool_or_false,
    _normalize_label,
    _parse_intent,
    _parse_json,
    _read_handshake_message,
    _send_handshake_message,
    _send_response,
    _str_or_empty,
)

if TYPE_CHECKING:
    from ..offloader import OffloaderController
    from ..receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)


class _HandshakeStep(StrEnum):
    """
    The three Noise XX handshake messages, in order.

    Threaded as a label-typed argument through the wire I/O
    helpers so log lines and timeout-error messages identify the
    specific step; ``MSG1`` / ``MSG2`` / ``MSG3`` rather than the
    Noise-spec tokens for grep-readability in logs.
    """

    MSG1 = "msg1"
    MSG2 = "msg2"
    MSG3 = "msg3"


@dataclass(frozen=True)
class _DispatchInput:
    """Per-session inputs to :func:`_dispatch_intent`; frozen, read-only."""

    intent: PeerLinkIntent
    dashboard_id: str
    label: str
    pin_sha256: str
    static_x25519_pub: bytes
    peer_ip: str
    pairing_key: str | None = None
    friendly_name: str = ""
    ha_addon: bool = False
    label_auto: bool = False
    # ``peer_link`` msg3's ``connect_back_port``; 0 when absent.
    connect_back_port: int = 0
    # ``connect_back`` msg3's ``peer_link_port``; 0 when absent.
    announced_peer_link_port: int = 0


async def _drive_peer_link_session(  # noqa: PLR0911 — the early-returns are the handshake's natural failure cliffs
    controller: ReceiverController,
    ws: web.WebSocketResponse,
    peer_ip: str,
    identity_priv: bytes,
    *,
    offloader: OffloaderController | None = None,
    accept_receiver_intents: bool = True,
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
        # sees the rejection in an authenticated frame, not a raw
        # transport close.
        if not await _send_handshake_message(session, ws, b"", _HandshakeStep.MSG2):
            return
        if await _read_handshake_message(session, ws, _HandshakeStep.MSG3) is None:
            return
        _LOGGER.warning("peer-link handshake from %s rejected (reason=bad_intent)", peer_ip)
        own_friendly_name, own_ha_addon = dashboard_display_identity(controller._db)
        await _send_response(
            session,
            ws,
            IntentResponse.REJECTED,
            reason=RejectReason.BAD_INTENT,
            friendly_name=own_friendly_name,
            ha_addon=own_ha_addon,
        )
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
    pairing_key = _str_or_empty(msg3.get("pairing_key")) or None
    # Offloader display identity; absent on older offloaders.
    # ``_normalize_label`` bounds the value — it lands on disk
    # and on the event bus, same trust surface as ``label``.
    peer_friendly_name = _normalize_label(msg3.get("friendly_name"))
    peer_ha_addon = _bool_or_false(msg3.get("ha_addon"))
    label_auto = _bool_or_false(msg3.get("label_auto"))
    connect_back_port = port_or_zero(msg3.get("connect_back_port"))
    announced_peer_link_port = port_or_zero(msg3.get("peer_link_port"))

    outcome = await _dispatch_intent(
        controller,
        _DispatchInput(
            intent=intent,
            dashboard_id=dashboard_id,
            label=label,
            pin_sha256=pin,
            static_x25519_pub=remote_static_pub,
            peer_ip=peer_ip,
            pairing_key=pairing_key,
            friendly_name=peer_friendly_name,
            ha_addon=peer_ha_addon,
            label_auto=label_auto,
            connect_back_port=connect_back_port,
            announced_peer_link_port=announced_peer_link_port,
        ),
        offloader=offloader,
        accept_receiver_intents=accept_receiver_intents,
    )
    # Log the decision, not the bare handshake; "ok" here used to
    # mean only "Noise XX completed", which read as "pairing
    # accepted" while the dispatch below still rejected.
    if outcome.response is IntentResponse.REJECTED:
        _LOGGER.warning(
            "peer-link %s from %s rejected (dashboard_id=%s reason=%s observed_offloader_pin=%s)",
            intent.value,
            peer_ip,
            dashboard_id,
            outcome.reason.value if outcome.reason is not None else "unspecified",
            pin,
        )
    else:
        _LOGGER.info(
            "peer-link %s from %s -> %s (dashboard_id=%s observed_offloader_pin=%s)",
            intent.value,
            peer_ip,
            outcome.response.value,
            dashboard_id,
            pin,
        )
    # Tell a previewing offloader this is a key-mode (--remote-build-only)
    # server, so its UI can require the bootstrap key up front. Static on
    # the server's mode, NOT on whether a window is currently armed —
    # always-true for a headless server reveals only "uses a key", never
    # "a window is open right now", so the window-open state stays secret.
    # Preview only; a pair_request refusal never carries it.
    requires_pairing_key = (
        intent is PeerLinkIntent.PREVIEW and controller._db.settings.remote_build_only
    )
    own_friendly_name, own_ha_addon = dashboard_display_identity(controller._db)
    await _send_response(
        session,
        ws,
        outcome.response,
        reason=outcome.reason,
        requires_pairing_key=requires_pairing_key,
        friendly_name=own_friendly_name,
        ha_addon=own_ha_addon,
    )

    # Hand off to the long-lived application session on an
    # OK-authed peer_link; every other intent (incl. REJECTED
    # peer_link) closes the WS via the handler's ``finally``.
    if intent is PeerLinkIntent.PEER_LINK and outcome.response is IntentResponse.OK:
        await _run_peer_link_session(
            controller=controller,
            ws=ws,
            session=session,
            dashboard_id=dashboard_id,
            peer_ip=peer_ip,
            peer_friendly_name=peer_friendly_name,
            peer_ha_addon=peer_ha_addon,
            peer_connect_back_port=connect_back_port,
        )


async def _dispatch_intent(  # noqa: PLR0911 — one return per intent branch
    controller: ReceiverController,
    inp: _DispatchInput,
    *,
    offloader: OffloaderController | None = None,
    accept_receiver_intents: bool = True,
) -> IntentOutcome:
    """
    Resolve a single peer-link intent into a typed :class:`IntentOutcome`.

    Pure dispatch — callable directly from tests without the WS /
    Noise plumbing. The WS driver has already validated the wire
    string into a :class:`PeerLinkIntent`; unknown wire values map
    to ``REJECTED`` before reaching here. ``connect_back`` routes
    to *offloader* and skips the dashboard_id gate (the pin is the
    key); every receiver intent is refused with ``BAD_INTENT``
    when *accept_receiver_intents* is false (offloader-only bind).
    """
    if inp.intent is PeerLinkIntent.CONNECT_BACK:
        if offloader is None:
            _LOGGER.warning("connect_back from %s with no offloader wired; rejecting", inp.peer_ip)
            return IntentOutcome(IntentResponse.REJECTED, RejectReason.NO_APPROVED_PEER)
        return await offloader._handle_connect_back(
            pin_sha256=inp.pin_sha256,
            peer_ip=inp.peer_ip,
            announced_port=inp.announced_peer_link_port,
        )
    if not accept_receiver_intents:
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.BAD_INTENT)
    if inp.intent is PeerLinkIntent.PREVIEW:
        # Preview captures the responder's static pubkey via the
        # handshake transcript; no controller call + no
        # dashboard_id needed.
        return IntentOutcome(IntentResponse.OK)

    # Reject malformed dashboard_id before any controller call.
    # Same alphabet + length contract as
    # :func:`controllers.remote_build._validators.validate_dashboard_id`
    # on the WS-command path; both consume from
    # ``helpers.dashboard_identity`` so they can't drift.
    if (
        not inp.dashboard_id
        or len(inp.dashboard_id) > DASHBOARD_ID_MAX_CHARS
        or not DASHBOARD_ID_PATTERN.fullmatch(inp.dashboard_id)
    ):
        return IntentOutcome(IntentResponse.REJECTED, RejectReason.BAD_DASHBOARD_ID)

    if inp.intent is PeerLinkIntent.PAIR_REQUEST:
        # Pairing-window gate lives inside ``record_pair_request``,
        # not here — only new-admin-authorization cases (new PENDING
        # row or pubkey rotation) should short-circuit on the window.
        # A re-pair against an already-APPROVED row whose pubkey
        # matches bypasses the window check.
        return await controller.record_pair_request(
            dashboard_id=inp.dashboard_id,
            pin_sha256=inp.pin_sha256,
            static_x25519_pub=inp.static_x25519_pub,
            label=inp.label,
            peer_ip=inp.peer_ip,
            pairing_key=inp.pairing_key,
            friendly_name=inp.friendly_name,
            ha_addon=inp.ha_addon,
            label_auto=inp.label_auto,
        )
    if inp.intent is PeerLinkIntent.PEER_LINK:
        return await controller.lookup_peer_for_session(
            dashboard_id=inp.dashboard_id, pin_sha256=inp.pin_sha256
        )
    # PeerLinkIntent.PAIR_STATUS — exhaustive enum match.
    return await controller.lookup_peer_for_status(
        dashboard_id=inp.dashboard_id, pin_sha256=inp.pin_sha256
    )
