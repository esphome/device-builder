"""
Peer-link Noise WS handler for the remote-build feature (issue #106).

Owns the wire shape of the
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
:meth:`PeerLinkNoiseSession.decrypt`. The session loop runs an
encrypted ``ping`` / ``pong`` heartbeat (30s tick + 90s miss
threshold) and a controller-side session registry dedupes by
``dashboard_id`` (a duplicate connect kicks the older session
via a ``terminate`` frame so a restarted offloader takes over
its previous slot rather than doubling). Application message
types (``submit_job``, ``job_state_changed``, ``queue_status``,
…) ride on top of the same Noise transport.

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
from typing import TYPE_CHECKING, Any, cast

from aiohttp import WSMsgType, web

from ....api.ws import WEBSOCKETS_KEY
from ....helpers.dashboard_identity import DASHBOARD_ID_MAX_CHARS, DASHBOARD_ID_PATTERN
from ....helpers.peer_link_identity import get_or_create_peer_link_identity
from ....helpers.peer_link_noise import (
    NOISE_ERRORS,
    HandshakeNotCompleteError,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
)
from ....models import (
    IntentResponse,
    PeerLinkIntent,
    SubmitJobChunkFrameData,
    SubmitJobFrameData,
)

# Redundant aliases mark these as intentional re-exports for both
# ruff (F401) and mypy (no-redef) — preserves external imports like
# ``from .peer_link import TerminateReason`` without an ``__all__``
# that would also accidentally narrow ``import *`` semantics.
from .wire import AppMessageType as AppMessageType
from .wire import TerminateReason as TerminateReason
from .wire_io import (
    _normalize_label,
    _parse_intent,
    _parse_json,
    _read_handshake_message,
    _send_handshake_message,
    _send_response,
    _str_or_empty,
)


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
    from ..receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)

PEER_LINK_PATH = "/remote-build/peer-link"

# Heartbeat cadence for the long-lived peer-link session. The
# receiver sends an encrypted ``ping`` frame every 30s and expects
# the offloader to echo it back with a ``pong`` carrying the same
# ``nonce``. Three consecutive missed pongs (90s of silence) close
# the session so a half-open TCP connection — common on LANs with
# dropped routes / sleeping middleboxes — doesn't pin a session
# slot indefinitely. Picked to match the receiver-pinged 30s /
# 90s-miss pattern called out in the issue's "Connection
# lifecycle" section.
HEARTBEAT_INTERVAL_SECONDS = 30.0
HEARTBEAT_MISS_THRESHOLD = 3
HEARTBEAT_DEAD_AFTER_SECONDS = HEARTBEAT_INTERVAL_SECONDS * HEARTBEAT_MISS_THRESHOLD

# Cap inbound application-frame size at 60 KiB. The cap is
# applied to the WS BINARY frame's bytes (``msg.data``) before
# Noise decrypt, so it bounds ciphertext + AEAD tag, not
# plaintext. The Noise framework spec's hard ceiling is 65535
# bytes per encrypted frame; 60 KiB leaves ~4 KiB headroom for
# the AEAD tag (16 bytes) plus any future protocol overhead.
#
# 5c bundle chunks are the actual sizing driver: a 32 KiB raw
# slice base64-inflates to ~43 KiB inside a JSON envelope
# around 43.5 KiB, fitting under 60 KiB with ~16 KiB further
# headroom for unusually long ``job_id`` / header fields.
# Smaller messages (heartbeat, queue_status, pair frames) are
# tiny (~30-200 bytes) and unaffected.
#
# The cap keeps a misbehaving / hostile peer from pinning
# memory before the dispatch loop sees the frame; raised from
# the original 32 KiB now that 5c has actual sizing
# requirements (the original comment anticipated this bump).
# Forward-compatible: smaller frames are always accepted.
APP_FRAME_MAX_BYTES = 60 * 1024


async def make_peer_link_handler(
    controller: ReceiverController,
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
        # Register on the peer-link app's WS set so the shared
        # ``close_active_websockets`` shutdown hook can unblock
        # this handler instead of pinning ``runner.cleanup()`` to
        # aiohttp's 60s ``shutdown_timeout`` while an idle paired
        # offloader sits in ``async for msg in ws``. The set is
        # seeded in :meth:`DeviceBuilder._build_and_start_remote_build_runner`
        # at construction time; tests that build a hand-rolled
        # peer-link app are expected to seed it themselves.
        request.app[WEBSOCKETS_KEY].add(ws)
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


# ---------------------------------------------------------------------------
# Long-lived peer-link session (post-handshake, ``intent="peer_link"`` only)
# ---------------------------------------------------------------------------


@dataclass
class PeerLinkSession:
    """
    State for one active receiver-side peer-link WS session.

    Owned by :class:`ReceiverController` (registered via
    :meth:`register_peer_link_session`, dropped via
    :meth:`unregister_peer_link_session`). Held while the
    underlying handler coroutine is running its receive loop;
    cleared the moment the loop returns.

    Composes a :class:`PeerLinkChannel` for the wire-level
    encrypt / send / parse / terminate operations — the same
    channel shape the offloader-side :class:`PeerLinkClient`
    uses, so both ends share one validation / framing seam.
    Sends from the controller (e.g. ``queue_status`` pushes in
    5b) go through :meth:`send_app_frame`; the
    :attr:`_closing` short-circuit there protects against a
    heartbeat / app sender racing a final frame onto the wire
    after :meth:`terminate` has flipped the close decision.
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
    _channel: PeerLinkChannel = field(init=False)

    def __post_init__(self) -> None:
        """Build the wire-level :class:`PeerLinkChannel` over (noise, ws)."""
        self._channel = PeerLinkChannel(noise=self.noise, ws=self.ws, log_label=self.dashboard_id)

    async def send_app_frame(self, payload: dict[str, Any]) -> bool:
        """Encrypt + send under the channel's lock; gated on ``_closing``.

        Returns ``True`` on success, ``False`` on encrypt /
        WS-side failure or once :meth:`terminate` has flipped
        the close decision (a heartbeat / app sender that wakes
        from ``asyncio.sleep`` after a controller-driven close
        mustn't race a final ``ping`` onto the wire after the
        ``terminate`` frame). The terminate frame itself routes
        through the channel directly — :meth:`PeerLinkChannel.send_terminate`
        bypasses the gate.
        """
        if self._closing:
            return False
        return await self._channel.send_frame(payload)

    async def terminate(self, reason: TerminateReason) -> None:
        """
        Send a ``terminate`` frame and close the WS.

        Idempotent. Used by the controller's session-registry
        dedupe path (kick the older session on a duplicate
        connect) and by ``stop()`` (drain everything before
        shutdown).

        Sets :attr:`_closing` *before* delegating to
        :meth:`PeerLinkChannel.send_terminate` so any racing
        :meth:`send_app_frame` call short-circuits cleanly; the
        terminate-frame send itself goes through the channel
        directly, bypassing the gate.
        """
        if self._closing:
            return
        self._closing = True
        await self._channel.send_terminate(reason.value)


async def _run_peer_link_session(
    controller: ReceiverController,
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
    :data:`HEARTBEAT_INTERVAL_SECONDS`, the offloader replies
    with a ``pong`` carrying the same ``nonce``, three consecutive
    misses (:data:`HEARTBEAT_DEAD_AFTER_SECONDS` of silence) close
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
    peer_link_session.last_pong_at = _monotonic()

    async def _send_ping(nonce: int) -> bool:
        return await peer_link_session.send_app_frame(
            {"type": AppMessageType.PING.value, "nonce": nonce}
        )

    async def _on_dead() -> None:
        await peer_link_session.terminate(TerminateReason.HEARTBEAT_TIMEOUT)

    heartbeat_task = asyncio.create_task(
        run_peer_link_heartbeat(
            send_ping=_send_ping,
            last_pong_at=lambda: peer_link_session.last_pong_at,
            on_dead=_on_dead,
        ),
        name=f"peer-link-heartbeat[{dashboard_id}]",
    )
    try:
        await _receive_loop(peer_link_session, controller)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        controller.unregister_peer_link_session(peer_link_session)


async def _receive_loop(session: PeerLinkSession, controller: ReceiverController) -> None:
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
    terminate). Dispatches ``ping`` / ``pong`` / ``terminate``
    keepalive frames plus ``submit_job`` / ``submit_job_chunk``
    against the controller's :class:`SubmitJobReceiver`. Other
    types log at debug and are ignored.
    """
    async for msg in session.ws:
        parsed = session._channel.parse_frame(msg)
        if parsed is None:
            await session.terminate(TerminateReason.MALFORMED_FRAME)
            return
        msg_type = parsed.get("type")
        if msg_type == AppMessageType.PONG.value:
            session.last_pong_at = _monotonic()
            continue
        if msg_type == AppMessageType.PING.value:
            # Mirror the offloader's ping nonce so a peer that
            # also runs heartbeat from its end gets pong
            # parity without us defining a separate keepalive
            # protocol per direction.
            nonce = parsed.get("nonce")
            await session.send_app_frame({"type": AppMessageType.PONG.value, "nonce": nonce})
            continue
        if msg_type == AppMessageType.TERMINATE.value:
            # Peer-initiated close. Don't echo a terminate back;
            # the WS will drain via the next ``CLOSE`` frame.
            session._closing = True
            return
        if msg_type == AppMessageType.SUBMIT_JOB.value:
            # Header validation lives inside the receiver. The
            # `cast` here is the dispatch boundary's "the wire
            # parse said this is a submit_job; treat it as the
            # matching TypedDict" hand-off — runtime field
            # validation is the receiver's job.
            await controller.get_submit_job_receiver().handle_submit_job(
                session, cast(SubmitJobFrameData, parsed)
            )
            continue
        if msg_type == AppMessageType.SUBMIT_JOB_CHUNK.value:
            await controller.get_submit_job_receiver().handle_submit_job_chunk(
                session, cast(SubmitJobChunkFrameData, parsed)
            )
            continue
        if msg_type == AppMessageType.CANCEL_JOB.value:
            # 5d cooperative cancel from the offloader. Frame
            # carries the offloader-supplied ``job_id`` we
            # stashed as ``FirmwareJob.remote_job_id`` at
            # submit time; the controller's handler resolves
            # it back through :class:`JobFanout` and routes
            # through ``FirmwareController.cancel``. No ack
            # frame — the resulting ``JOB_CANCELLED`` bus
            # event fans out a ``job_state_changed{cancelled}``
            # which the offloader already plumbs.
            await controller.handle_cancel_job(session, parsed)
            continue
        if msg_type == AppMessageType.DOWNLOAD_ARTIFACTS.value:
            # Offloader is requesting the built-firmware
            # artifact set for a previously-completed job. The
            # sender packs idedata.json + every flash image
            # listed in ``idedata.flash_images`` into a
            # gzipped tarball + streams it back via
            # ``artifacts_start`` → chunks → ``artifacts_end``.
            await controller.get_artifacts_download_sender().handle_download_artifacts(
                session, parsed
            )
            continue
        _LOGGER.debug(
            "peer-link unknown app frame type %r from %s; ignoring",
            msg_type,
            session.dashboard_id,
        )


def parse_app_frame(
    noise: PeerLinkNoiseSession, msg: Any, *, log_label: str
) -> dict[str, Any] | None:
    """
    Validate, decrypt, and JSON-parse one inbound peer-link frame.

    Returns the parsed dict on success or ``None`` on any of the
    malformed-frame branches: wrong WS message type (not BINARY),
    oversize body, Noise decrypt failure, or post-decrypt JSON
    that isn't an object. Concentrating the per-branch logging
    here keeps each side's dispatch loop a single straight line —
    receiver and offloader callers both respond to ``None`` by
    closing the session (the offloader maps it to
    ``transport_error``, the receiver to a structured
    ``terminate{malformed_frame}`` frame).

    Public so the offloader-side :class:`PeerLinkClient` can share
    the same validation seam with the receiver-side
    :class:`PeerLinkSession` without duplicating the four
    log-and-return branches. ``log_label`` is what each side wants
    in its log lines — the receiver passes its
    ``dashboard_id``, the offloader passes
    ``"<hostname>:<port>"``.
    """
    if msg.type != WSMsgType.BINARY:
        _LOGGER.debug(
            "peer-link expected binary frame from %s; got %s",
            log_label,
            msg.type,
        )
        return None
    if len(msg.data) > APP_FRAME_MAX_BYTES:
        _LOGGER.warning(
            "peer-link oversize frame from %s (%d bytes); closing",
            log_label,
            len(msg.data),
        )
        return None
    try:
        plaintext = noise.decrypt(msg.data)
    except NOISE_ERRORS:
        _LOGGER.warning(
            "peer-link Noise decrypt failed from %s",
            log_label,
            exc_info=True,
        )
        return None
    parsed = _parse_json(plaintext)
    if not isinstance(parsed, dict):
        _LOGGER.debug(
            "peer-link frame from %s did not decode to a JSON object",
            log_label,
        )
        return None
    return parsed


async def run_peer_link_heartbeat(
    *,
    send_ping: Callable[[int], Awaitable[bool]],
    last_pong_at: Callable[[], float],
    on_dead: Callable[[], Awaitable[None]],
) -> None:
    """
    Run a heartbeat loop driving either end of a peer-link session.

    Sleeps :data:`HEARTBEAT_INTERVAL_SECONDS`, then either bails
    out via *on_dead* (if no pong has landed within
    :data:`HEARTBEAT_DEAD_AFTER_SECONDS`) or sends a ping via
    *send_ping*. ``send_ping`` returns whether the wire write
    succeeded; a ``False`` return triggers *on_dead* too (the WS
    is presumed dead so the session shouldn't keep trying).

    Lets :class:`asyncio.CancelledError` propagate out of
    ``asyncio.sleep`` — callers spawn this as a task and cancel
    it under ``contextlib.suppress(CancelledError)``; catching
    here would swallow the signal at the wrong layer.

    Each side's *on_dead* is what differs: the receiver sends a
    structured ``terminate{heartbeat_timeout}`` via the session
    registry's close path, the offloader just calls
    ``ws.close()`` (the receive loop sees the close and unwinds
    naturally). Each side's *send_ping* is what gates the send —
    the receiver routes through :meth:`PeerLinkSession.send_app_frame`
    so the ``_closing`` short-circuit holds; the offloader routes
    through :meth:`PeerLinkChannel.send_frame` directly because
    its lifecycle has no equivalent gate.
    """
    nonce = 0
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        # Liveness check first — if we haven't heard a pong in
        # the threshold window, bail before sending another ping.
        if _monotonic() - last_pong_at() > HEARTBEAT_DEAD_AFTER_SECONDS:
            await on_dead()
            return
        nonce += 1
        if not await send_ping(nonce):
            # send_ping already logged; the WS is presumed dead.
            await on_dead()
            return


def _monotonic() -> float:
    """Indirection so tests can monkey-patch the clock under the heartbeat loop."""
    return asyncio.get_running_loop().time()


# Re-export at the bottom: ``channel.py``'s lazy import looks up
# ``parse_app_frame`` as a package attribute, so the channel module
# must load after it's defined above.
from .channel import PeerLinkChannel as PeerLinkChannel  # noqa: E402
