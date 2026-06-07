"""
Long-lived peer-link session (post-handshake, ``intent="peer_link"`` only).

Owns :class:`PeerLinkSession`, the receive loop, the encrypted
ping / pong heartbeat, and the inbound-frame validator
``parse_app_frame`` shared by both wire ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from aiohttp import WSMsgType, web

from ....helpers.peer_link_noise import NOISE_ERRORS, PeerLinkNoiseSession
from ....models import SubmitJobChunkFrameData, SubmitJobFrameData
from .channel import PeerLinkChannel
from .wire import AppMessageType, TerminateReason
from .wire_io import _parse_json

if TYPE_CHECKING:
    from ..receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)

# Receiver-driven heartbeat: ping every 30s; three consecutive
# missed pongs (90s of silence) close the session so a half-open
# TCP connection on a flaky LAN can't pin a slot indefinitely.
HEARTBEAT_INTERVAL_SECONDS = 30.0
HEARTBEAT_MISS_THRESHOLD = 3
HEARTBEAT_DEAD_AFTER_SECONDS = HEARTBEAT_INTERVAL_SECONDS * HEARTBEAT_MISS_THRESHOLD

# Cap inbound application-frame size at 60 KiB (ciphertext + AEAD
# tag, before Noise decrypt). Noise's 65535-byte ceiling minus
# ~4 KiB headroom. 5c bundle chunks (32 KiB raw → ~43 KiB after
# b64+JSON envelope) drove the size; smaller frame types are
# unaffected. Bounds memory before the dispatch loop sees the
# frame, so a hostile peer can't pin arbitrary bytes.
APP_FRAME_MAX_BYTES = 60 * 1024


@dataclass
class PeerLinkSession:
    """
    State for one active receiver-side peer-link WS session.

    Owned by :class:`ReceiverController` via
    register / unregister_peer_link_session. Composes a
    :class:`PeerLinkChannel` for wire-level encrypt / send /
    parse / terminate. The :attr:`_closing` short-circuit on
    :meth:`send_app_frame` protects against a heartbeat / app
    sender racing a final frame onto the wire after
    :meth:`terminate` has flipped the close decision.
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

        Returns ``False`` once :meth:`terminate` has flipped the
        close decision so a heartbeat / app sender that wakes from
        ``asyncio.sleep`` after the close can't race a final frame
        onto the wire. The terminate frame itself routes through
        :meth:`PeerLinkChannel.send_terminate`, which bypasses the
        gate.
        """
        if self._closing:
            return False
        return await self._channel.send_frame(payload)

    async def terminate(self, reason: TerminateReason) -> None:
        """Send a ``terminate`` frame and close the WS; idempotent.

        Sets :attr:`_closing` *before* delegating to
        :meth:`PeerLinkChannel.send_terminate` so any racing
        :meth:`send_app_frame` call short-circuits cleanly. The
        terminate-frame send goes through the channel directly,
        bypassing the gate.
        """
        if self._closing:
            return
        self._closing = True
        await self._channel.send_terminate(reason.value)


def parse_app_frame(
    noise: PeerLinkNoiseSession, msg: Any, *, log_label: str
) -> dict[str, Any] | None:
    """
    Validate, decrypt, and JSON-parse one inbound peer-link frame.

    Returns the parsed dict on success or ``None`` on any
    malformed branch (non-BINARY type, oversize body, Noise
    decrypt failure, post-decrypt JSON that isn't an object).
    Both wire ends call this so the four log-and-return branches
    live in one place; receiver maps ``None`` to a structured
    ``terminate{malformed_frame}``, offloader to a
    ``transport_error``.
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
    Heartbeat loop driving either end of a peer-link session.

    Sleeps :data:`HEARTBEAT_INTERVAL_SECONDS`, then bails via
    *on_dead* (no pong within :data:`HEARTBEAT_DEAD_AFTER_SECONDS`)
    or sends a ping via *send_ping*. A ``False`` *send_ping*
    return also triggers *on_dead* — the WS is presumed dead.

    Lets ``asyncio.CancelledError`` propagate out of
    ``asyncio.sleep``; callers cancel as a task under
    ``contextlib.suppress(CancelledError)``.
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


async def _run_peer_link_session(
    controller: ReceiverController,
    ws: web.WebSocketResponse,
    session: PeerLinkNoiseSession,
    dashboard_id: str,
    peer_ip: str,
) -> None:
    """
    Run the post-handshake receive loop + heartbeat for one session.

    Returns on peer close, heartbeat timeout, controller shutdown,
    or a malformed frame. Always unregisters the session in its
    ``finally`` so cleanup runs even on uncaught exceptions.
    """
    peer_link_session = PeerLinkSession(
        dashboard_id=dashboard_id,
        ws=ws,
        noise=session,
        peer_ip=peer_ip,
    )
    # Register before spawning the heartbeat — a duplicate connect
    # on the same loop tick must find this session in the registry
    # to kick it. ``register_peer_link_session`` does the dedupe
    # synchronously, so the registration is observed atomically.
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

    Returns on peer close, malformed frame (after firing the
    structured ``terminate``), or controller-driven session
    close. Dispatches keepalive frames + ``submit_job`` /
    ``submit_job_chunk`` / ``cancel_job`` / ``download_artifacts``
    against the controller; unknown types log at debug and are
    ignored. ``WebSocketResponse`` is async-iterable so CLOSE /
    CLOSING / ERROR exit cleanly without explicit handling.
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
            # Mirror the offloader's nonce so a peer that runs
            # heartbeat from its end gets pong parity without a
            # per-direction keepalive protocol.
            nonce = parsed.get("nonce")
            await session.send_app_frame({"type": AppMessageType.PONG.value, "nonce": nonce})
            continue
        if msg_type == AppMessageType.TERMINATE.value:
            # Peer-initiated close. Don't echo a terminate back;
            # the WS will drain via the next ``CLOSE`` frame.
            session._closing = True
            return
        handler = _APP_FRAME_DISPATCH.get(msg_type) if isinstance(msg_type, str) else None
        if handler is not None:
            await handler(controller, session, parsed)
            continue
        _LOGGER.debug(
            "peer-link unknown app frame type %r from %s; ignoring",
            msg_type,
            session.dashboard_id,
        )


async def _dispatch_submit_job(
    controller: ReceiverController, session: PeerLinkSession, parsed: dict[str, Any]
) -> None:
    # The cast is the dispatch boundary's "wire said submit_job; treat as the
    # matching TypedDict" hand-off; the receiver validates the fields.
    await controller.get_submit_job_receiver().handle_submit_job(
        session, cast(SubmitJobFrameData, parsed)
    )


async def _dispatch_submit_job_chunk(
    controller: ReceiverController, session: PeerLinkSession, parsed: dict[str, Any]
) -> None:
    await controller.get_submit_job_receiver().handle_submit_job_chunk(
        session, cast(SubmitJobChunkFrameData, parsed)
    )


async def _dispatch_cancel_job(
    controller: ReceiverController, session: PeerLinkSession, parsed: dict[str, Any]
) -> None:
    # Cooperative cancel; fire-and-forget. The resulting JOB_CANCELLED bus
    # event fans out job_state_changed{cancelled} which the offloader already
    # plumbs, so no ack frame is needed.
    await controller.handle_cancel_job(session, parsed)


async def _dispatch_download_artifacts(
    controller: ReceiverController, session: PeerLinkSession, parsed: dict[str, Any]
) -> None:
    # Streams the completed build's artifacts back via artifacts_start,
    # chunks, then artifacts_end.
    await controller.get_artifacts_download_sender().handle_download_artifacts(session, parsed)


# Inbound app-frame type → async handler. PING / PONG / TERMINATE branch in
# the receive loop body; they touch session-local state or close the loop and
# don't fit the shared handler shape. Malformed frames branch upstream.
_APP_FRAME_DISPATCH: dict[
    str, Callable[[ReceiverController, PeerLinkSession, dict[str, Any]], Awaitable[None]]
] = {
    AppMessageType.SUBMIT_JOB.value: _dispatch_submit_job,
    AppMessageType.SUBMIT_JOB_CHUNK.value: _dispatch_submit_job_chunk,
    AppMessageType.CANCEL_JOB.value: _dispatch_cancel_job,
    AppMessageType.DOWNLOAD_ARTIFACTS.value: _dispatch_download_artifacts,
}


def _monotonic() -> float:
    """Indirection so tests can monkey-patch the clock under the heartbeat loop."""
    return asyncio.get_running_loop().time()
