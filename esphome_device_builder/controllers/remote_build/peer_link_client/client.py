"""
Long-lived offloader-side peer-link Noise WS session.

:class:`PeerLinkClient` is the one-per-pairing initiator that
opens the long-lived ``intent="peer_link"`` WS, runs the Noise XX
handshake via :func:`.one_shot._drive_initiator_handshake_and_read_response`,
parks on a receive loop with an encrypted heartbeat, and reconnects
with bounded backoff on every close other than a receiver-side
``superseded``. Submit-job / cancel-job / artifact-download flows
ride the same channel; inbound frames fan out to bus events the
offloader controller and the firmware fan-out listen to.

The one-shot initiator helpers (``preview_pair`` / ``request_pair``
/ ``await_pair_status``) live in :mod:`.one_shot` — same handshake,
different lifetime shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

import aiohttp
from yarl import URL

from ....helpers import json as _json
from ....helpers.async_ import drain_tasks
from ....helpers.peer_link_noise import (
    NOISE_ERRORS,
    PeerLinkNoiseSession,
    pin_sha256_for_pubkey,
    public_bytes_for_priv,
)
from ....helpers.peer_link_resolver import _SkipHostsResolver, make_peer_link_http_session
from ....models import (
    IntentResponse,
    PeerLinkIntent,
    RejectReason,
    SubmitJobAckFrameData,
)
from .._client_models import (
    DownloadArtifactsResult,
    SubmitJobSessionLostError,
    _DownloadArtifactsState,
    _SessionLoopState,
)
from ..peer_link import (
    APP_FRAME_MAX_BYTES,
    PEER_LINK_PATH,
    AppMessageType,
    PeerLinkChannel,
    TerminateReason,
    run_peer_link_heartbeat,
)
from . import _dispatch, _submit
from .one_shot import (
    _DEFAULT_TIMEOUT_SECONDS,
    _drive_initiator_handshake_and_read_response,
    _extract_auto_provision_supported,
    _extract_receiver_esphome_version,
)

if TYPE_CHECKING:
    from aiohttp.resolver import AbstractResolver

    from ....helpers.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)


# 1s initial keeps a transient drop (LAN flap, brief receiver
# restart) from looking like a hang; 30s cap keeps an extended
# outage from spamming the receiver's accept queue. Reset on
# every successful connect.
_RECONNECT_INITIAL_BACKOFF_SECONDS = 1.0
_RECONNECT_MAX_BACKOFF_SECONDS = 30.0


# Per-reason recovery policy: a receiver ``reason`` maps to
# ``(orphan?, operator-facing message)``. A reason absent here
# (older receiver, unknown / non-string) falls through to the
# transient default, so the client keeps reconnecting.
_REJECT_DEFAULT: tuple[bool, str] = (False, "auth rejected")
_REJECT_INFO: dict[str, tuple[bool, str]] = {
    RejectReason.PIN_MISMATCH.value: (
        True,
        "rejected: receiver has a different key on file (re-pair)",
    ),
    RejectReason.NO_APPROVED_PEER.value: (
        True,
        "rejected: receiver has no approval on file (re-pair)",
    ),
    RejectReason.PENDING_NOT_APPROVED.value: (
        False,
        "waiting for the receiver to accept the pairing",
    ),
}


# Offloader-side close reasons (wire-level ones live in
# :class:`TerminateReason`). Surfaced verbatim in the
# ``OFFLOADER_PEER_LINK_CLOSED`` event so subscribers can
# distinguish "we lost the connection" from "the receiver
# kicked us."
_LOCAL_CLOSE_TRANSPORT_ERROR = "transport_error"
_LOCAL_CLOSE_HEARTBEAT_TIMEOUT = "heartbeat_timeout"
_LOCAL_CLOSE_CLIENT_STOPPED = "client_stopped"
_LOCAL_CLOSE_PEER_HUNG_UP = "peer_hung_up"
_LOCAL_CLOSE_AUTH_REJECTED = "auth_rejected"
# Terminal ``intent_response: rejected`` (``no_approved_peer`` /
# ``pin_mismatch``). Orphans rather than reconnecting so a doomed
# handshake doesn't hammer the receiver every 30s; re-pair / unpair
# is the recovery. A reason-less or ``pending_not_approved`` reject
# stays on the reconnect path.
_LOCAL_CLOSE_RECEIVER_REJECTED = "receiver_rejected"
# Receiver's post-handshake pubkey didn't match the OOB-confirmed
# value — either legitimate rotation or a MITM / mDNS spoof.
# Aborts before any application frames flow and orphans so the
# reconnect loop doesn't hammer the wrong endpoint; operator
# recovery is re-pair or unpair.
_LOCAL_CLOSE_PIN_MISMATCH = "pin_mismatch"


class PeerLinkClient:
    """
    Long-lived offloader-side peer-link Noise WS session.

    One instance per APPROVED :class:`StoredPairing`, owned by
    :class:`OffloaderController`. Drive via :meth:`run`
    (cancellable asyncio task): connects to the receiver's
    peer-link port, runs the Noise XX handshake with
    ``intent="peer_link"``, parks on a receive loop with an
    encrypted heartbeat, and reconnects on any close other than
    a receiver-side ``superseded`` (which would loop forever
    against whatever instance now holds our slot).

    Cancelling :meth:`run` is the controller-side teardown path —
    the finally chain sends ``terminate{reason: client_stopped}``
    so the receiver's session loop unwinds cleanly without
    waiting for its heartbeat to time out.
    """

    def __init__(
        self,
        *,
        receiver_hostname: str,
        receiver_port: int,
        identity_priv: bytes,
        dashboard_id: str,
        pinned_static_x25519_pub: bytes,
        pin_sha256: str,
        receiver_label: str,
        bus: EventBus,
        resolver: AbstractResolver | None = None,
    ) -> None:
        self._hostname = receiver_hostname
        self._port = receiver_port
        self._identity_priv = identity_priv
        self._identity_pub = public_bytes_for_priv(identity_priv)
        # Peer IPs we've self-loopbacked against. Read live by the
        # resolver wrapper below so the next reconnect picks a
        # different A record from aiohttp's cached resolution.
        # IPv4-shaped: IPv6 doesn't manifest the docker-bridge
        # self-loopback shape (no shared default ULA prefix) so
        # we don't normalise v6 representations here.
        self._self_loopback_ips: set[str] = set()
        # ``None`` falls back to aiohttp's default resolver — the
        # only viable shape for unit tests that don't construct a
        # real Zeroconf.
        self._http_resolver: AbstractResolver | None = (
            _SkipHostsResolver(resolver, self._self_loopback_ips) if resolver is not None else None
        )
        self._dashboard_id = dashboard_id
        # Compared against ``session.remote_static_pub``
        # post-handshake on every connect so an attacker with
        # their own keypair can't complete Noise XX against this
        # client. ``pin_sha256`` is the same value hashed, used
        # to key into pin-keyed offloader state.
        # ``receiver_label`` lets the pin-mismatch alert name
        # the row at firing time.
        self._pinned_static_x25519_pub = pinned_static_x25519_pub
        self._pin_sha256 = pin_sha256
        self._receiver_label = receiver_label
        self._bus = bus
        # True after a receiver-side ``terminate{superseded}`` or
        # post-handshake pin-mismatch — reconnecting would just
        # hammer the wrong endpoint. One-shot: never cleared on
        # the instance. Controller recovers by dropping this
        # client and constructing a fresh :class:`PeerLinkClient`.
        self._orphaned = False
        # True once a session reached ``intent_response: ok`` —
        # :meth:`run`'s backoff resets only on previously-opened
        # sessions; never-opened cycles advance exponentially so
        # a broken receiver doesn't get hammered.
        self._session_was_opened = False
        # Set inside :meth:`_run_session_loops` before parking;
        # cleared in its ``finally``. :meth:`submit_job` reads
        # this to raise :class:`PeerLinkNoSessionError` when the
        # session's gone. Single writer (run task), single reader
        # (controller's WS submit handler), same event loop — no
        # lock needed.
        self._active_channel: PeerLinkChannel | None = None
        # Per-job ack futures populated by :meth:`submit_job`
        # before the header goes out, drained by the receive
        # loop's submit_job_ack dispatch, force-completed with
        # :class:`SubmitJobSessionLostError` in
        # :meth:`_run_session_loops`'s ``finally`` on session
        # loss so callers don't hang on the ack timeout.
        self._submit_job_acks: dict[str, asyncio.Future[SubmitJobAckFrameData]] = {}
        # Operator-facing "Last connection error" line. Populated
        # by :meth:`_run_one_session`'s exception paths, cleared
        # on every successful session-open so a stale message
        # doesn't survive a reconnect.
        self._last_connect_error: str = ""
        # Per-job in-flight download state — same drain shape as
        # ``_submit_job_acks`` (force-completed on session loss).
        self._artifacts_downloads: dict[str, _DownloadArtifactsState] = {}

    @property
    def receiver_hostname(self) -> str:
        return self._hostname

    @property
    def receiver_port(self) -> int:
        return self._port

    @property
    def pin_sha256(self) -> str:
        """OOB-verified pin (sha256 of the receiver's pubkey)."""
        return self._pin_sha256

    @property
    def is_session_open(self) -> bool:
        """True if a peer-link session is currently live (post-handshake, dispatch parked)."""
        return self._active_channel is not None

    @property
    def is_orphaned(self) -> bool:
        """
        True if the run loop has been poisoned and won't reconnect.

        Set on receiver-side ``superseded`` close (another
        offloader instance with the same ``dashboard_id`` took
        our slot) or post-handshake pin-mismatch. One-shot:
        never cleared on this instance. Operator recovery is
        re-pair or unpair; the controller drops this client and
        constructs a fresh one.
        """
        return self._orphaned

    @property
    def is_connecting(self) -> bool:
        """
        True if the run loop is alive but no session is currently open.

        Tri-state with :attr:`is_session_open` (connected) and
        :attr:`is_orphaned` (terminal-until-re-pair). UI renders
        "Connecting…" / "Connected" / "Disconnected (last error: …)"
        from the three properties.
        """
        return not self._orphaned and not self.is_session_open

    @property
    def last_connect_error(self) -> str:
        """
        Most-recent connection failure as a one-line description.

        ``"{ExcType}: {msg}"`` for transport / Noise failures,
        ``"auth rejected"`` for handshake rejections,
        ``"pin mismatch"`` on orphan-on-rotation. Cleared on
        every successful session-open. Empty on a
        never-connected pairing.
        """
        return self._last_connect_error

    async def submit_job(
        self,
        *,
        job_id: str,
        configuration_filename: str,
        target: Literal["compile", "upload", "clean"],
        bundle_bytes: bytes,
        device_name: str = "",
        device_friendly_name: str = "",
        target_esphome_version: str = "",
    ) -> SubmitJobAckFrameData:
        return await _submit.submit_job(
            self,
            job_id=job_id,
            configuration_filename=configuration_filename,
            target=target,
            bundle_bytes=bundle_bytes,
            device_name=device_name,
            device_friendly_name=device_friendly_name,
            target_esphome_version=target_esphome_version,
        )

    async def cancel_job(self, *, job_id: str) -> bool:
        return await _submit.cancel_job(self, job_id=job_id)

    async def download_artifacts(self, *, job_id: str) -> DownloadArtifactsResult:
        return await _submit.download_artifacts(self, job_id=job_id)

    async def run(self) -> None:
        """
        Run the connect-loop forever. Cancellable.

        Each iteration opens a WS, drives Noise XX with
        ``intent="peer_link"``, parks on the receive loop with a
        heartbeat, fires OPENED / CLOSED bus events on every
        transition, and reconnects with exponential backoff
        (interrupted on cancellation). ``superseded`` /
        ``pin_mismatch`` closes orphan the client and exit.
        Cancellation sends ``terminate{reason: client_stopped}``
        via the inner session's handler before propagating.
        """
        backoff = _RECONNECT_INITIAL_BACKOFF_SECONDS
        try:
            while not self._orphaned:
                close_reason = await self._run_one_session()
                # ``_last_connect_error`` carries the specific
                # failure detail alongside the category-level
                # ``reason`` for clean operator-facing UX.
                self._fire_closed(close_reason, error_detail=self._last_connect_error)
                if close_reason == TerminateReason.SUPERSEDED.value:
                    _LOGGER.info(
                        "peer-link client to %s:%d (%r) superseded by another instance "
                        "with the same dashboard_id; orphaning",
                        self._hostname,
                        self._port,
                        self._receiver_label,
                    )
                    self._orphaned = True
                    return
                if close_reason in (_LOCAL_CLOSE_PIN_MISMATCH, _LOCAL_CLOSE_RECEIVER_REJECTED):
                    # Detection site in :meth:`_run_one_session` logs
                    # the warning + fires the alert; this branch only
                    # owns the orphan transition.
                    self._orphaned = True
                    return
                # Reset on session that reached ``intent_response: ok``
                # so a flaky path doesn't permanently degrade to
                # the cap; advance exponentially otherwise so a
                # broken receiver isn't hammered every second.
                if self._session_was_opened:
                    backoff = _RECONNECT_INITIAL_BACKOFF_SECONDS
                else:
                    backoff = min(backoff * 2, _RECONNECT_MAX_BACKOFF_SECONDS)
                await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            # ``_run_one_session`` already sent the structured
            # ``terminate`` frame in its own CancelledError handler
            # (where the WS and Noise session are still live as
            # locals). Fire the bus event so subscribers see the
            # transition; no-op for subscribers that key off
            # OPENED first.
            self._fire_closed(_LOCAL_CLOSE_CLIENT_STOPPED)
            raise

    async def _run_one_session(self) -> str:
        """
        Run one connect → handshake → receive loop iteration.

        Returns the close reason. Exceptions are caught and
        mapped onto a local close reason; ``CancelledError`` is
        the one exception that propagates (the run loop's outer
        handler fires the bus event).
        """
        self._session_was_opened = False
        url = URL.build(scheme="ws", host=self._hostname, port=self._port, path=PEER_LINK_PATH)
        # ``total`` deliberately omitted — peer-link is idle-by-
        # design once parked on the receive loop, so a session-wide
        # timeout would forcibly drop a healthy session.
        # Handshake reads are bounded with ``asyncio.wait_for``
        # downstream so a stalled handshake still fails fast.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=_DEFAULT_TIMEOUT_SECONDS)
        try:
            async with (
                make_peer_link_http_session(timeout=timeout, resolver=self._http_resolver) as http,
                http.ws_connect(url, max_msg_size=APP_FRAME_MAX_BYTES) as ws,
            ):
                peer = ws.get_extra_info("peername")
                _LOGGER.info(
                    "peer-link client connected to %s:%d (%r, peer=%s)",
                    self._hostname,
                    self._port,
                    self._receiver_label,
                    peer,
                )
                session = PeerLinkNoiseSession.initiator(self._identity_priv)
                msg3_payload = _json.dumps({"dashboard_id": self._dashboard_id})
                response_ct = await _drive_initiator_handshake_and_read_response(
                    ws=ws,
                    sess=session,
                    intent=PeerLinkIntent.PEER_LINK,
                    msg3_payload=msg3_payload,
                    read_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
                )
                # Pin-check BEFORE decrypting / acting on the
                # response. Noise XX authenticates that the
                # responder holds the private key for the pubkey
                # it advertised; a mismatched pubkey means a
                # legitimate rotation or a MITM / mDNS spoof.
                # Abort either way before any application frames.
                observed = session.remote_static_pub
                if observed == self._identity_pub:
                    return self._on_self_static_observed(peer)
                if observed != self._pinned_static_x25519_pub:
                    # ``stored_pin`` is the sha256 written to disk at
                    # pair time; ``expected_pin`` is what the raw
                    # pinned bytes hash to right now. They must agree
                    # (set from the same ``result.remote_static_pub``
                    # in :meth:`OffloaderController.request_pair`);
                    # logging both surfaces a divergence as a
                    # stored-row corruption symptom instead of a
                    # wire-level one.
                    _LOGGER.warning(
                        "peer-link client to %s:%d (%r) observed pin drift "
                        "(stored_pin=%s expected_pin=%s expected_bytes=%s "
                        "observed_pin=%s observed_bytes=%s); orphaning "
                        "until the operator re-pairs or unpairs",
                        self._hostname,
                        self._port,
                        self._receiver_label,
                        self._pin_sha256,
                        pin_sha256_for_pubkey(self._pinned_static_x25519_pub),
                        self._pinned_static_x25519_pub.hex(),
                        pin_sha256_for_pubkey(observed),
                        observed.hex(),
                    )
                    self._fire_pin_mismatch(observed=observed)
                    self._last_connect_error = "pin mismatch"
                    return _LOCAL_CLOSE_PIN_MISMATCH
                response = _json.loads(session.decrypt(response_ct))
                if (
                    not isinstance(response, dict)
                    or response.get("intent_response") != IntentResponse.OK.value
                ):
                    return self._on_handshake_rejected(response)
                receiver_version = _extract_receiver_esphome_version(response)
                auto_provision = _extract_auto_provision_supported(response)
                channel = PeerLinkChannel(
                    noise=session, ws=ws, log_label=f"{self._hostname}:{self._port}"
                )
                self._session_was_opened = True
                self._last_connect_error = ""
                self._fire_opened(
                    esphome_version=receiver_version,
                    auto_provision_supported=auto_provision,
                )
                try:
                    return await self._run_session_loops(channel)
                except asyncio.CancelledError:
                    # Best-effort structured close before the WS
                    # goes away under us. ``send_terminate`` skips
                    # the ``_closing`` gate — this terminate IS
                    # the close — so the frame goes out reliably.
                    await channel.send_terminate(_LOCAL_CLOSE_CLIENT_STOPPED)
                    raise
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError, TypeError) as exc:
            _LOGGER.debug(
                "peer-link client to %s:%d (%r) transport error: %s",
                self._hostname,
                self._port,
                self._receiver_label,
                exc,
                exc_info=True,
            )
            self._last_connect_error = f"{type(exc).__name__}: {exc}"
            return _LOCAL_CLOSE_TRANSPORT_ERROR
        except NOISE_ERRORS as exc:
            _LOGGER.warning(
                "peer-link client to %s:%d (%r) Noise failure: %s",
                self._hostname,
                self._port,
                self._receiver_label,
                exc,
                exc_info=True,
            )
            self._last_connect_error = f"{type(exc).__name__}: {exc}"
            return _LOCAL_CLOSE_TRANSPORT_ERROR

    async def _run_session_loops(self, channel: PeerLinkChannel) -> str:  # noqa: C901
        """
        Run the receive loop with a heartbeat task in parallel.

        Returns the close reason. The receive loop and heartbeat
        share a :class:`_SessionLoopState`: receive bumps
        ``last_pong_at`` on pong and writes ``close_reason`` on
        transport-error / terminate / unknown-msg-type exits;
        heartbeat's ``_on_dead`` writes ``HEARTBEAT_TIMEOUT`` so
        the reason reflects the real cause.
        """
        state = _SessionLoopState(
            last_pong_at=asyncio.get_running_loop().time(),
            close_reason=_LOCAL_CLOSE_PEER_HUNG_UP,
        )

        async def _send_ping(nonce: int) -> bool:
            return await channel.send_frame({"type": AppMessageType.PING.value, "nonce": nonce})

        async def _on_dead() -> None:
            state.close_reason = _LOCAL_CLOSE_HEARTBEAT_TIMEOUT
            _LOGGER.info(
                "peer-link client to %s:%d (%r) heartbeat timeout; closing",
                self._hostname,
                self._port,
                self._receiver_label,
            )
            # ``ws.close()`` can raise ``ClientConnectionError`` /
            # ``ClientError`` when the peer has already gone
            # away; letting that escape would crash the heartbeat
            # task and fall through to the ``peer_hung_up``
            # default, masking the real cause.
            with contextlib.suppress(OSError, RuntimeError, aiohttp.ClientError):
                await channel.ws.close()

        heartbeat_task = asyncio.create_task(
            run_peer_link_heartbeat(
                send_ping=_send_ping,
                last_pong_at=lambda: state.last_pong_at,
                on_dead=_on_dead,
            ),
            name=f"peer-link-client-heartbeat[{self._hostname}:{self._port}]",
        )
        # Expose the channel to :meth:`submit_job` for the
        # session's duration. Cleared in ``finally`` so a
        # post-session submit raises :class:`PeerLinkNoSessionError`
        # instead of writing into a stale channel.
        self._active_channel = channel
        # Built once per session for the receive-loop hot path —
        # one dict lookup per frame. PING / PONG / TERMINATE /
        # malformed touch session-local state or close the loop
        # and stay branched in the loop body.
        sync_dispatch = self._build_sync_frame_dispatch()
        try:
            async for msg in channel.ws:
                parsed = channel.parse_frame(msg)
                if parsed is None:
                    # ``parse_frame`` already logged per-branch
                    # context for the malformed-frame case.
                    state.close_reason = _LOCAL_CLOSE_TRANSPORT_ERROR
                    break
                msg_type = parsed.get("type")
                if msg_type == AppMessageType.PING.value:
                    nonce = parsed.get("nonce")
                    await channel.send_frame({"type": AppMessageType.PONG.value, "nonce": nonce})
                    continue
                if msg_type == AppMessageType.PONG.value:
                    state.last_pong_at = asyncio.get_running_loop().time()
                    continue
                if msg_type == AppMessageType.TERMINATE.value:
                    reason = parsed.get("reason")
                    state.close_reason = (
                        reason if isinstance(reason, str) else _LOCAL_CLOSE_PEER_HUNG_UP
                    )
                    break
                handler = sync_dispatch.get(msg_type) if isinstance(msg_type, str) else None
                if handler is not None:
                    handler(parsed)
                    continue
                _LOGGER.debug(
                    "peer-link client unknown app frame type %r from %s:%d; ignoring",
                    msg_type,
                    self._hostname,
                    self._port,
                )
            return state.close_reason
        finally:
            self._active_channel = None
            # Drain in-flight submitters so they raise
            # :class:`SubmitJobSessionLostError` immediately
            # instead of waiting on the per-flow timeout. Snapshot
            # the dict before iterating — :meth:`submit_job`'s
            # ``finally`` pops the entry as soon as the future fires.
            for pending_job_id, pending_fut in list(self._submit_job_acks.items()):
                if not pending_fut.done():
                    pending_fut.set_exception(
                        SubmitJobSessionLostError(
                            f"submit_job: peer-link session to "
                            f"{self._hostname}:{self._port} ended before ack "
                            f"for job_id={pending_job_id!r}"
                        )
                    )
            # Same drain shape for in-flight artifact downloads.
            for pending_job_id, dl_state in list(self._artifacts_downloads.items()):
                if not dl_state.future.done():
                    dl_state.future.set_exception(
                        SubmitJobSessionLostError(
                            f"download_artifacts: peer-link session to "
                            f"{self._hostname}:{self._port} ended before "
                            f"artifacts_end for job_id={pending_job_id!r}"
                        )
                    )
            await drain_tasks((heartbeat_task,))

    def _build_sync_frame_dispatch(
        self,
    ) -> dict[str, Callable[[dict[str, Any]], None]]:
        """
        Return the inbound-frame → sync handler map for one session.

        PING / PONG / TERMINATE are excluded — they mutate
        :class:`_SessionLoopState` or close the loop and don't
        fit the ``(parsed) -> None`` shape. Malformed frames
        branch upstream of this lookup.
        """
        return {
            AppMessageType.QUEUE_STATUS.value: self._dispatch_queue_status,
            AppMessageType.SUBMIT_JOB_ACK.value: self._dispatch_submit_job_ack,
            AppMessageType.JOB_STATE_CHANGED.value: self._dispatch_job_state_changed,
            AppMessageType.JOB_OUTPUT.value: self._dispatch_job_output,
            AppMessageType.ARTIFACTS_START.value: self._dispatch_artifacts_start,
            AppMessageType.ARTIFACTS_CHUNK.value: self._dispatch_artifacts_chunk,
            AppMessageType.ARTIFACTS_END.value: self._dispatch_artifacts_end,
        }

    def _dispatch_queue_status(self, parsed: dict[str, Any]) -> None:
        _dispatch.dispatch_queue_status(self, parsed)

    def _dispatch_submit_job_ack(self, parsed: dict[str, Any]) -> None:
        _dispatch.dispatch_submit_job_ack(self, parsed)

    def _log_malformed(self, frame_type: str, parsed: dict[str, Any]) -> None:
        _dispatch.log_malformed(self, frame_type, parsed)

    def _dispatch_job_state_changed(self, parsed: dict[str, Any]) -> None:
        _dispatch.dispatch_job_state_changed(self, parsed)

    def _dispatch_job_output(self, parsed: dict[str, Any]) -> None:
        _dispatch.dispatch_job_output(self, parsed)

    def _dispatch_artifacts_start(self, parsed: dict[str, Any]) -> None:
        _dispatch.dispatch_artifacts_start(self, parsed)

    def _dispatch_artifacts_chunk(self, parsed: dict[str, Any]) -> None:
        _dispatch.dispatch_artifacts_chunk(self, parsed)

    def _dispatch_artifacts_end(self, parsed: dict[str, Any]) -> None:
        _dispatch.dispatch_artifacts_end(self, parsed)

    def _fire_opened(
        self, *, esphome_version: str = "", auto_provision_supported: bool = False
    ) -> None:
        _dispatch.fire_opened(
            self,
            esphome_version=esphome_version,
            auto_provision_supported=auto_provision_supported,
        )

    def _fire_closed(self, reason: str, *, error_detail: str = "") -> None:
        _dispatch.fire_closed(self, reason, error_detail=error_detail)

    def _fire_pin_mismatch(self, *, observed: bytes) -> None:
        _dispatch.fire_pin_mismatch(self, observed=observed)

    def _fire_peer_revoked(self) -> None:
        _dispatch.fire_peer_revoked(self)

    def _fire_queue_status(self, *, idle: bool, running: bool, queue_depth: int) -> None:
        _dispatch.fire_queue_status(self, idle=idle, running=running, queue_depth=queue_depth)

    def _on_handshake_rejected(self, response: Any) -> str:
        """Map a non-OK ``intent_response`` to a close reason; orphan on terminal reasons."""
        reason = response.get("reason") if isinstance(response, dict) else None
        key = reason if isinstance(reason, str) else ""
        terminal, message = _REJECT_INFO.get(key, _REJECT_DEFAULT)
        self._last_connect_error = message
        if terminal:
            _LOGGER.warning(
                "peer-link client to %s:%d (%r) rejected by receiver (reason=%s); orphaning "
                "until the operator re-pairs or unpairs",
                self._hostname,
                self._port,
                self._receiver_label,
                reason,
            )
            self._fire_peer_revoked()
            return _LOCAL_CLOSE_RECEIVER_REJECTED
        _LOGGER.warning(
            "peer-link client to %s:%d (%r) rejected at handshake: %r",
            self._hostname,
            self._port,
            self._receiver_label,
            response,
        )
        return _LOCAL_CLOSE_AUTH_REJECTED

    def _on_self_static_observed(self, peer: Any) -> str:
        """Skip *peer*'s IP next resolve, log ERROR, return the transport-error close reason."""
        if isinstance(peer, tuple) and peer and isinstance(peer[0], str):
            self._self_loopback_ips.add(peer[0])
        _LOGGER.error(
            "peer-link client to %s:%d (%r) observed our own static pubkey from the responder "
            "(peer=%s pin=%s); check mDNS / routing (hostname resolves to one of this "
            "host's own IPs) or identity collision (receiver running with a copy of our "
            "peer-link key)",
            self._hostname,
            self._port,
            self._receiver_label,
            peer,
            self._pin_sha256,
        )
        self._last_connect_error = "self loopback"
        return _LOCAL_CLOSE_TRANSPORT_ERROR
