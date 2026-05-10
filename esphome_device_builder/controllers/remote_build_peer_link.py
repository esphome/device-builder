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
``{"intent_response": "..."}``. For ``intent="preview"`` /
``"pair_request"`` / ``"pair_status"`` the receiver then closes
the WS — those intents are one-shot. For ``intent="peer_link"``
on a successful auth (``IntentResponse.OK``), the receiver
*keeps the WS open* and runs a long-lived application session
on top of the same Noise transport: every subsequent frame is
JSON-encoded then ChaCha20-Poly1305-encrypted via
:meth:`PeerLinkNoiseSession.encrypt` /
:meth:`PeerLinkNoiseSession.decrypt`. Phase 5a-1 lands the
session loop with heartbeat (encrypted ``ping`` / ``pong``,
30s tick + 90s miss threshold) and a controller-side session
registry that dedupes by ``dashboard_id`` (a duplicate connect
kicks the older session via a ``terminate`` frame so a restarted
offloader takes over its previous slot rather than doubling).
Application message types (``submit_job``, ``job_state_changed``,
``queue_status``, …) land in subsequent 5b-5d PRs against this
foundation.

Timeouts: handshake reads have an explicit timeout so a peer that
opens a TCP connection and never sends the first frame can't pin
a coroutine forever. The timeout is generous (10s) because the
Noise XX handshake itself is local-DH cheap; only the network
round-trip costs anything, and that's bounded by LAN latency.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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

# Heartbeat cadence for the long-lived peer-link session. The
# receiver sends an encrypted ``ping`` frame every 30s and expects
# the offloader to echo it back with a ``pong`` carrying the same
# ``nonce``. Three consecutive missed pongs (90s of silence) close
# the session so a half-open TCP connection — common on LANs with
# dropped routes / sleeping middleboxes — doesn't pin a session
# slot indefinitely. Picked to match the receiver-pinged 30s /
# 90s-miss pattern called out in the issue's "Connection
# lifecycle" section.
_HEARTBEAT_INTERVAL_SECONDS = 30.0
_HEARTBEAT_MISS_THRESHOLD = 3
_HEARTBEAT_DEAD_AFTER_SECONDS = _HEARTBEAT_INTERVAL_SECONDS * _HEARTBEAT_MISS_THRESHOLD

# Cap inbound application-frame size at 32 KiB. Heartbeat frames
# are tiny (~30 bytes); the offloader has no legitimate reason to
# send larger frames in 5a-1 (no application messages defined yet),
# and the cap keeps a misbehaving / hostile peer from pinning
# memory before the dispatch loop sees the frame. Bundle upload in
# 5c gets its own larger streaming cap on a per-message-type
# basis. The hard ceiling from the Noise framework spec is
# 65535 bytes per frame; staying well under that leaves headroom
# for the protocol overhead and a future relax-the-cap change.
_APP_FRAME_MAX_BYTES = 32 * 1024


class TerminateReason(StrEnum):
    """
    Wire ``reason`` value on a structured ``terminate`` close frame.

    Sent inside an :attr:`_AppMessageType.TERMINATE` application
    frame so the offloader's reconnect logic (5a-2) can branch
    on the reason rather than guessing from the WS close code.

    * ``SUPERSEDED`` — a fresh peer-link connect from the same
      ``dashboard_id`` displaces this older session. Standard
      "restarted offloader" path.
    * ``HEARTBEAT_TIMEOUT`` — three pings in a row without a
      matching pong. The session loop closes itself; the wire
      frame may not actually reach the peer (TCP is presumed
      dead) but the WS close is still graceful from the
      receiver's side.
    * ``SERVER_SHUTTING_DOWN`` — the receiver controller is
      stopping. Sent to every active session before
      :meth:`RemoteBuildController.stop` returns.
    * ``MALFORMED_FRAME`` — a frame fails Noise decrypt /
      JSON parse / shape validation. Closes the session
      immediately; peer can reconnect after the next handshake.
    """

    SUPERSEDED = "superseded"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    SERVER_SHUTTING_DOWN = "server_shutting_down"
    MALFORMED_FRAME = "malformed_frame"


class _AppMessageType(StrEnum):
    """
    Wire ``type`` discriminator on post-handshake application frames.

    JSON-encoded plaintext is wrapped in a ChaCha20-Poly1305
    transport frame via the established Noise session (one frame
    per WS message) before going on the wire.

    5a-1 lands only the heartbeat + close types; 5b-5d add
    ``queue_status`` / ``submit_job`` / ``job_state_changed`` /
    ``job_output`` / ``cancel_job`` against the same dispatch
    seam.
    """

    PING = "ping"
    PONG = "pong"
    TERMINATE = "terminate"


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


# ---------------------------------------------------------------------------
# Long-lived peer-link session (post-handshake, ``intent="peer_link"`` only)
# ---------------------------------------------------------------------------


@dataclass
class PeerLinkSession:
    """
    State for one active receiver-side peer-link WS session.

    Owned by :class:`RemoteBuildController` (registered via
    :meth:`register_peer_link_session`, dropped via
    :meth:`unregister_peer_link_session`). Held while the
    underlying handler coroutine is running its receive loop;
    cleared the moment the loop returns.

    The Noise session is the post-handshake instance that already
    burned msg1 / msg2 / msg3 — every subsequent
    :meth:`PeerLinkNoiseSession.encrypt` /
    :meth:`PeerLinkNoiseSession.decrypt` call wraps a
    ChaCha20-Poly1305 transport frame on a fresh nonce. The
    session is single-threaded by virtue of the asyncio loop;
    application sends from the receiver controller (queue_status
    pushes in 5b, etc.) need to await :meth:`send_app_frame` so
    the encrypt + WS-write pair is atomic.
    """

    dashboard_id: str
    ws: web.WebSocketResponse
    noise: PeerLinkNoiseSession
    peer_ip: str
    # Loop-monotonic timestamp of the most recent pong (or session
    # start if no pong has landed yet). The heartbeat loop seeds
    # this just before its first sleep so a slow first pong
    # doesn't trip the miss threshold instantly.
    last_pong_at: float = 0.0
    # Set by :meth:`terminate` when something other than the
    # session loop's natural exit (peer close, heartbeat timeout)
    # closes the session — used by the loop to skip the
    # heartbeat-timeout terminate frame on a path where the
    # caller already sent its own.
    _closing: bool = False
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send_app_frame(self, payload: dict[str, Any]) -> bool:
        """
        Encrypt *payload* (JSON-encoded) and send it as a binary WS frame.

        Returns ``True`` on success, ``False`` on encrypt error /
        WS-side failure. The send lock serialises concurrent
        callers (heartbeat + future application-message senders)
        so the Noise nonce advances in one direction only — the
        Noise cipher state is not safe to share across concurrent
        encrypts.

        Short-circuits to ``False`` once :meth:`terminate` has set
        :attr:`_closing` so a heartbeat / app sender that wakes
        from ``asyncio.sleep`` after the controller-driven close
        decision can't race a final ``ping`` onto the wire after
        the ``terminate`` frame has already gone out. The
        ``terminate`` frame itself bypasses this gate via
        :meth:`_send_app_frame_unchecked`.
        """
        if self._closing:
            return False
        return await self._send_app_frame_unchecked(payload)

    async def _send_app_frame_unchecked(self, payload: dict[str, Any]) -> bool:
        """Encrypt + send without the ``_closing`` short-circuit.

        Internal helper for :meth:`terminate` so the structured
        close frame still goes out even after ``_closing`` is
        set. Public application sends route through
        :meth:`send_app_frame`.
        """
        try:
            plaintext = _json.dumps(payload)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "peer-link app frame for %s failed JSON encode", self.dashboard_id, exc_info=True
            )
            return False
        async with self._send_lock:
            try:
                ciphertext = self.noise.encrypt(plaintext)
            except NOISE_ERRORS:
                _LOGGER.warning(
                    "peer-link app frame for %s failed Noise encrypt",
                    self.dashboard_id,
                    exc_info=True,
                )
                return False
            return await _send_bytes_safely(self.ws, ciphertext, log_label="app frame")

    async def terminate(self, reason: TerminateReason) -> None:
        """
        Send a ``terminate`` frame and close the WS.

        Idempotent. Used by the controller's session-registry
        dedupe path (kick the older session on a duplicate
        connect) and by ``stop()`` (drain everything before
        shutdown). Best-effort — a peer that has already gone
        away won't receive the frame, and the close itself
        swallows transport errors.

        Sets :attr:`_closing` *before* sending the terminate
        frame so any racing :meth:`send_app_frame` call
        short-circuits cleanly; the terminate-frame send itself
        bypasses the gate via :meth:`_send_app_frame_unchecked`.
        """
        if self._closing:
            return
        self._closing = True
        await self._send_app_frame_unchecked(
            {"type": _AppMessageType.TERMINATE.value, "reason": reason.value}
        )
        # Narrow suppress to transport-level errors. Python 3.8+
        # made ``CancelledError`` inherit from ``BaseException``
        # so ``Exception`` already wouldn't catch it, but being
        # explicit about which classes we expect on a
        # closing-an-already-dead-WS path keeps the intent
        # legible.
        with contextlib.suppress(OSError, RuntimeError):
            await self.ws.close()


async def _run_peer_link_session(
    controller: RemoteBuildController,
    ws: web.WebSocketResponse,
    session: PeerLinkNoiseSession,
    dashboard_id: str,
    peer_ip: str,
) -> None:
    """
    Run the post-handshake receive loop + heartbeat for one peer-link session.

    Returns when the session ends — peer close, heartbeat
    timeout, controller shutdown, or a malformed frame. Always
    cleans up the controller-side registration in its ``finally``
    so a session is unregistered the moment its coroutine exits,
    even on uncaught exceptions.

    Heartbeat is receiver-driven (per the issue's "Connection
    lifecycle" spec): the receiver pings every
    :data:`_HEARTBEAT_INTERVAL_SECONDS`, the offloader replies
    with a ``pong`` carrying the same ``nonce``, three consecutive
    misses (:data:`_HEARTBEAT_DEAD_AFTER_SECONDS` of silence) close
    the session.
    """
    peer_link_session = PeerLinkSession(
        dashboard_id=dashboard_id,
        ws=ws,
        noise=session,
        peer_ip=peer_ip,
    )
    # Register before spawning the heartbeat — a duplicate connect
    # arriving in the same loop tick MUST find this session in the
    # registry so it can kick it. The dedupe runs synchronously
    # inside :meth:`register_peer_link_session` so the
    # registration is observed atomically.
    await controller.register_peer_link_session(peer_link_session)
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(peer_link_session),
        name=f"peer-link-heartbeat[{dashboard_id}]",
    )
    try:
        await _receive_loop(peer_link_session)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        controller.unregister_peer_link_session(peer_link_session)


async def _receive_loop(session: PeerLinkSession) -> None:
    """
    Read frames off the WS, decrypt, parse, and dispatch.

    aiohttp's ``WebSocketResponse`` is async-iterable; the
    iterator yields only message frames (BINARY / TEXT / PING /
    PONG) and exits cleanly on CLOSE / CLOSING / ERROR, so we
    don't have to spell those transitions out.

    Returns on peer close, malformed frame (after firing the
    structured ``terminate``), or controller-driven session close
    (the registry's :meth:`PeerLinkSession.terminate` flips
    ``_closing`` so a CLOSE frame doesn't trigger a redundant
    terminate). 5a-1 dispatches only ``ping`` / ``pong`` /
    ``terminate`` — every other type logs at debug and is
    ignored. Application message types land against this same
    seam in 5b-5d.
    """
    async for msg in session.ws:
        parsed = _parse_app_frame(session, msg)
        if parsed is None:
            await session.terminate(TerminateReason.MALFORMED_FRAME)
            return
        msg_type = parsed.get("type")
        if msg_type == _AppMessageType.PONG.value:
            session.last_pong_at = _monotonic()
            continue
        if msg_type == _AppMessageType.PING.value:
            # Mirror the offloader's ping nonce so a peer that
            # also runs heartbeat from its end (5a-2) gets pong
            # parity without us defining a separate keepalive
            # protocol per direction.
            nonce = parsed.get("nonce")
            await session.send_app_frame({"type": _AppMessageType.PONG.value, "nonce": nonce})
            continue
        if msg_type == _AppMessageType.TERMINATE.value:
            # Peer-initiated close. Don't echo a terminate back;
            # the WS will drain via the next ``CLOSE`` frame.
            session._closing = True
            return
        _LOGGER.debug(
            "peer-link unknown app frame type %r from %s; ignoring",
            msg_type,
            session.dashboard_id,
        )


def _parse_app_frame(session: PeerLinkSession, msg: Any) -> dict[str, Any] | None:
    """
    Validate, decrypt, and JSON-parse one inbound frame.

    Returns the parsed dict on success or ``None`` on any of the
    malformed-frame branches: wrong WS message type (not BINARY),
    oversize body, Noise decrypt failure, or post-decrypt JSON
    that isn't an object. The caller (``_receive_loop``) responds
    to ``None`` with a structured ``terminate{malformed_frame}``
    close — concentrating the per-branch logging here keeps the
    dispatch loop a single straight line.
    """
    if msg.type != WSMsgType.BINARY:
        _LOGGER.debug(
            "peer-link expected binary frame from %s; got %s",
            session.dashboard_id,
            msg.type,
        )
        return None
    if len(msg.data) > _APP_FRAME_MAX_BYTES:
        _LOGGER.warning(
            "peer-link oversize frame from %s (%d bytes); closing",
            session.dashboard_id,
            len(msg.data),
        )
        return None
    try:
        plaintext = session.noise.decrypt(msg.data)
    except NOISE_ERRORS:
        _LOGGER.warning(
            "peer-link Noise decrypt failed from %s",
            session.dashboard_id,
            exc_info=True,
        )
        return None
    parsed = _parse_json(plaintext)
    if not isinstance(parsed, dict):
        _LOGGER.debug(
            "peer-link frame from %s did not decode to a JSON object",
            session.dashboard_id,
        )
        return None
    return parsed


async def _heartbeat_loop(session: PeerLinkSession) -> None:
    """
    Receiver-driven heartbeat: ping every interval, close on missed-pong threshold.

    Uses an integer ``nonce`` that bumps per ping so a future
    debug surface can correlate ping → pong (5a-1 doesn't read
    it, but echoing it is part of the contract). The pong landing
    sets :attr:`PeerLinkSession.last_pong_at`; if the gap from
    "now" exceeds :data:`_HEARTBEAT_DEAD_AFTER_SECONDS` after a
    ping, terminate with :attr:`TerminateReason.HEARTBEAT_TIMEOUT`.
    """
    session.last_pong_at = _monotonic()
    nonce = 0
    while True:
        # CancelledError from sleep() propagates out — the
        # parent coroutine in :func:`_run_peer_link_session`
        # cancels this task in its ``finally`` and awaits it
        # under ``contextlib.suppress(CancelledError)``.
        # Catching here would swallow the cancellation signal
        # at the wrong layer.
        await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        # Liveness check first — if we haven't heard a pong in
        # the threshold window, bail before sending another ping.
        if _monotonic() - session.last_pong_at > _HEARTBEAT_DEAD_AFTER_SECONDS:
            await session.terminate(TerminateReason.HEARTBEAT_TIMEOUT)
            return
        nonce += 1
        sent = await session.send_app_frame({"type": _AppMessageType.PING.value, "nonce": nonce})
        if not sent:
            # send_app_frame already logs; the WS is presumed
            # dead so close the session.
            await session.terminate(TerminateReason.HEARTBEAT_TIMEOUT)
            return


def _monotonic() -> float:
    """Indirection so tests can monkey-patch the clock under the heartbeat loop."""
    return asyncio.get_running_loop().time()


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
