"""
Long-lived offloader-side peer-link Noise WS session (issue #106).

:class:`PeerLinkClient` is the one-per-pairing initiator that
opens the long-lived ``intent="peer_link"`` WS, runs the Noise
XX handshake (via :func:`.one_shot._drive_initiator_handshake_and_read_response`),
parks on a receive loop with an encrypted heartbeat, and
reconnects with bounded backoff on every close other than a
receiver-side ``superseded``. Submit-job / cancel-job /
artifact-download flows ride the same channel; inbound frames
fan out to bus events the offloader controller and the
firmware fan-out listen to.

The one-shot initiator helpers (``preview_pair`` /
``request_pair`` / ``await_pair_status``) live in
:mod:`.one_shot` — same Noise XX handshake, different lifetime
shape.
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
from ....helpers.peer_link_noise import (
    NOISE_ERRORS,
    PeerLinkNoiseSession,
)
from ....helpers.peer_link_resolver import make_peer_link_http_session
from ....models import (
    IntentResponse,
    PeerLinkIntent,
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
    _extract_receiver_esphome_version,
)

if TYPE_CHECKING:
    from aiohttp.resolver import AbstractResolver

    from ....helpers.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)


# Auto-reconnect cadence after a session ends. Initial 1-second
# wait keeps a transient drop (LAN flap, brief receiver restart)
# from looking like a hang to the user; the 30s cap keeps an
# extended outage from spamming the receiver's accept queue.
# Reset to the initial value on every successful connect so a
# flaky path doesn't permanently degrade to the cap.
_RECONNECT_INITIAL_BACKOFF_SECONDS = 1.0
_RECONNECT_MAX_BACKOFF_SECONDS = 30.0


# Offloader-side close reasons that aren't on the wire (the
# wire-level reasons live in :class:`TerminateReason` — those
# come *from* the receiver). These describe close paths that
# originate on our side: transport error, our own heartbeat
# timeout, controller-initiated stop. Surfaced verbatim in the
# ``OFFLOADER_PEER_LINK_CLOSED`` event payload's ``reason``
# field so subscribers can distinguish "we lost the connection"
# from "the receiver kicked us."
_LOCAL_CLOSE_TRANSPORT_ERROR = "transport_error"
_LOCAL_CLOSE_HEARTBEAT_TIMEOUT = "heartbeat_timeout"
_LOCAL_CLOSE_CLIENT_STOPPED = "client_stopped"
_LOCAL_CLOSE_PEER_HUNG_UP = "peer_hung_up"
_LOCAL_CLOSE_AUTH_REJECTED = "auth_rejected"
# Receiver's static X25519 pubkey hash (from the live Noise XX
# handshake) didn't match the value the offloader OOB-confirmed
# at pair time. Either the receiver's identity legitimately
# rotated, or an attacker has interposed (e.g. mDNS spoof
# pointing the offloader at an attacker-controlled host that
# completed the handshake with its own keypair). The
# :class:`PeerLinkClient` aborts the connection before any
# application frames flow and orphans itself so the reconnect
# loop doesn't hammer the wrong endpoint; the operator's
# resolution is to re-pair (clearing the alert) or unpair
# (removing the row).
_LOCAL_CLOSE_PIN_MISMATCH = "pin_mismatch"


class PeerLinkClient:
    """
    Long-lived offloader-side peer-link Noise WS session.

    One instance per APPROVED :class:`StoredPairing`, owned by
    :class:`OffloaderController`. Drive via
    :meth:`run` (cancellable asyncio task) — connects to the
    receiver's peer-link port, runs the Noise XX handshake with
    ``intent="peer_link"``, parks on a receive loop, drives an
    encrypted heartbeat, and reconnects on any close other than
    a receiver-side ``superseded`` (which would loop forever
    against whatever instance now holds our slot).

    Bus events fire on every transition: ``OFFLOADER_PEER_LINK_OPENED``
    once the post-handshake ``intent_response: ok`` lands and
    the dispatch loop is parked, and ``OFFLOADER_PEER_LINK_CLOSED``
    on every clean exit (carries a ``reason`` so the offloader-
    side frontend Settings UI can branch on close cause).

    Cancelling the :meth:`run` task is the controller-side
    teardown path — the run loop's ``finally`` chain sends a
    ``terminate{reason: client_stopped}`` to the receiver before
    the WS closes so the receiver-side session loop unwinds
    cleanly without waiting for its heartbeat to time out.
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
        self._dashboard_id = dashboard_id
        # Shared :class:`aiohttp` resolver wired to the
        # dashboard's :class:`AsyncZeroconf` so ``.local``
        # receiver hostnames resolve through mDNS instead of the
        # OS resolver (which often doesn't have mDNS plumbed).
        # ``None`` falls back to ``aiohttp``'s default resolver,
        # which is the only viable shape for unit tests that
        # don't construct a real Zeroconf.
        self._resolver = resolver
        # Pinned receiver pubkey from the OOB-verified pair flow,
        # captured during ``preview_pair`` and stored on
        # :class:`StoredPairing.static_x25519_pub`. Compared
        # against ``session.remote_static_pub`` post-handshake on
        # every connect so an attacker with their own X25519
        # keypair can't complete Noise XX against this client and
        # reach the application channel. ``pin_sha256`` is the
        # SHA-256 of the same pubkey, carried on every event the
        # client fires so the controller's listener can key into
        # ``_open_peer_links`` / ``_offloader_alerts`` /
        # ``_peer_queue_status`` (pin-keyed offloader state).
        # ``receiver_label`` is carried so
        # the pin-mismatch alert can name the row at firing time.
        self._pinned_static_x25519_pub = pinned_static_x25519_pub
        self._pin_sha256 = pin_sha256
        self._receiver_label = receiver_label
        self._bus = bus
        # Set to True when we observe a receiver-side
        # ``terminate{reason: superseded}`` close — means a
        # newer offloader instance with the same dashboard_id
        # has taken our slot. Reconnecting would just collide
        # with that instance and trigger an endless flap, so
        # we orphan the run loop instead. The controller can
        # explicitly :meth:`run` again (e.g. after a config
        # reload) to reset.
        self._orphaned = False
        # Set ``True`` once a session reached
        # ``intent_response: ok`` and the dispatch loop parked.
        # The reconnect-backoff logic in :meth:`run` resets the
        # backoff window only when the previous session opened —
        # if we never got past the handshake (transport error,
        # auth rejected) the backoff advances exponentially so a
        # broken receiver doesn't get hammered.
        self._session_was_opened = False
        # Live :class:`PeerLinkChannel` for the currently-open
        # session, or ``None`` when between sessions. Set inside
        # :meth:`_run_session_loops` before the receive loop
        # parks, cleared in the same method's ``finally`` after
        # the loop exits. :meth:`submit_job` reads this to know
        # whether a session is live (raising
        # :class:`PeerLinkNoSessionError` if not) and to drive
        # the chunk send through the same channel the receive
        # loop is parked on. Only one writer (the run task) and
        # one reader (the controller's WS submit handler), both
        # on the same event loop, so no lock is needed.
        self._active_channel: PeerLinkChannel | None = None
        # Per-job ack futures, keyed on the ``job_id`` we put on
        # the ``submit_job`` header. Populated by
        # :meth:`submit_job` before the header goes out, drained
        # by the receive loop on the matching ``submit_job_ack``
        # frame, and force-completed in
        # :meth:`_run_session_loops`'s ``finally`` if the session
        # closes mid-flow (so ``submit_job`` doesn't hang on the
        # ack timeout when the wire is already gone). Future's
        # ``set_result`` value is the validated ack frame; on
        # session-loss the future gets
        # :class:`SubmitJobSessionLostError`.
        self._submit_job_acks: dict[str, asyncio.Future[SubmitJobAckFrameData]] = {}
        # Last-connection-failure description for the operator-
        # facing "Last connection error" line on the paired-rows
        # list. Populated in :meth:`_run_one_session`'s exception
        # paths with ``f"{type(exc).__name__}: {exc}"`` for
        # transport / Noise failures, ``"auth rejected"`` for the
        # post-handshake intent_response branch, and
        # ``"pin mismatch"`` for the orphan-on-rotation path.
        # Cleared when a session reaches the post-handshake open
        # state so a stale failure message doesn't survive a
        # successful reconnect. Empty on a never-connected pairing
        # where the client task hasn't completed its first attempt.
        self._last_connect_error: str = ""
        # Per-job download state for ``download_artifacts``.
        # Populated by :meth:`download_artifacts` before the
        # request goes out, drained by the receive loop's
        # ``artifacts_start`` / ``artifacts_chunk`` /
        # ``artifacts_end`` dispatch, and force-completed in
        # :meth:`_run_session_loops`'s ``finally`` on session
        # loss (same shape as ``_submit_job_acks``).
        # ``DownloadArtifactsState`` holds the in-flight
        # :class:`BundleAssembler` plus the result future.
        self._artifacts_downloads: dict[str, _DownloadArtifactsState] = {}

    @property
    def receiver_hostname(self) -> str:
        return self._hostname

    @property
    def receiver_port(self) -> int:
        return self._port

    @property
    def pin_sha256(self) -> str:
        """OOB-verified pin (sha256 of the receiver's pubkey).

        Stable identifier for this client — matches the key in
        :attr:`OffloaderController._peer_link_clients` and the
        ``pin_sha256`` field on every event this client fires.
        Surfaced as a property so the controller's WS handler
        can confirm it matches the request before driving a
        :meth:`submit_job`.
        """
        return self._pin_sha256

    @property
    def is_session_open(self) -> bool:
        """True if a peer-link session is currently live (post-handshake, dispatch parked)."""
        return self._active_channel is not None

    @property
    def is_orphaned(self) -> bool:
        """True if the run loop has been poisoned and won't reconnect.

        Set in two cases, both of which mean reconnecting would
        just hammer the wrong endpoint:

        * Receiver-side ``terminate{reason: superseded}`` close
          — a newer offloader instance with the same
          ``dashboard_id`` has taken our slot. Reconnecting
          would collide with that instance.
        * Pin-mismatch on the post-handshake pin-check —
          ``session.remote_static_pub`` didn't match the
          OOB-confirmed pubkey, so we're talking to a
          rotated-but-legitimate receiver or to an attacker.
          Either way the operator's resolution (re-pair to
          confirm the new identity, or unpair) is the only
          path forward.

        The controller's restart path (a fresh :meth:`run`)
        clears the flag.
        """
        return self._orphaned

    @property
    def is_connecting(self) -> bool:
        """True if the run loop is alive but no session is currently open.

        The ``True`` window covers both the very first connect
        attempt (``_run_one_session`` before the post-handshake
        ``intent_response: ok``) and every subsequent reconnect
        cycle inside :meth:`run`'s backoff loop. Goes ``False``
        in two distinct directions:

        * Forward to ``connected``: a session reached the
          post-handshake open state and parked on the receive
          loop. :meth:`is_session_open` returns ``True``.
        * Sideways to ``orphaned``: a pin-mismatch / superseded
          close poisoned the run loop. :meth:`is_orphaned`
          returns ``True``.

        UI uses the tri-state to render "Connected" /
        "Connecting…" / "Disconnected (last error: …)"; an
        orphaned client is the disconnected case where the
        operator has to re-pair or unpair to recover.
        """
        return not self._orphaned and not self.is_session_open

    @property
    def last_connect_error(self) -> str:
        """Most-recent connection failure as a one-line description.

        Set by :meth:`_run_one_session`'s exception paths to
        ``f"{type(exc).__name__}: {exc}"`` for transport / Noise
        failures, to ``"auth rejected"`` for handshake-rejected
        sessions, and to ``"pin mismatch"`` for the orphan-on-
        rotation path. Cleared when a session reaches the
        post-handshake open state — a stale message must not
        survive a successful reconnect.

        Empty on a never-connected pairing (the run loop hasn't
        completed its first attempt yet) and on cleanly-stopped
        clients (``client_stopped`` close on controller
        shutdown).
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
    ) -> SubmitJobAckFrameData:
        return await _submit.submit_job(
            self,
            job_id=job_id,
            configuration_filename=configuration_filename,
            target=target,
            bundle_bytes=bundle_bytes,
            device_name=device_name,
            device_friendly_name=device_friendly_name,
        )

    async def cancel_job(self, *, job_id: str) -> bool:
        return await _submit.cancel_job(self, job_id=job_id)

    async def download_artifacts(self, *, job_id: str) -> DownloadArtifactsResult:
        return await _submit.download_artifacts(self, job_id=job_id)

    async def run(self) -> None:
        """Run the connect-loop forever. Cancellable.

        Each iteration:

        1. Open WS, drive Noise XX with ``intent="peer_link"``.
        2. On ``intent_response: ok``, fire
           ``OFFLOADER_PEER_LINK_OPENED``, park on the receive
           loop with a heartbeat task running alongside.
        3. On any session end (receiver-side ``terminate``,
           heartbeat miss, transport error, peer-hung-up),
           fire ``OFFLOADER_PEER_LINK_CLOSED`` with the
           appropriate reason.
        4. If the close reason is ``superseded``, mark the
           client orphaned and exit. Otherwise sleep
           exponential-backoff (interrupted on cancellation)
           and loop.

        Cancellation at any point sends a structured
        ``terminate{reason: client_stopped}`` if a session is
        active, then propagates the ``CancelledError`` to the
        controller so the task drops cleanly.
        """
        backoff = _RECONNECT_INITIAL_BACKOFF_SECONDS
        try:
            while not self._orphaned:
                close_reason = await self._run_one_session()
                # ``_last_connect_error`` was populated by the
                # exception paths inside ``_run_one_session`` (or
                # left empty for clean closes — receiver-driven
                # ``terminate`` frames, heartbeat timeouts that
                # reach here without an exception, etc.). Pass it
                # through so the close event carries the specific
                # failure detail alongside the category-level
                # ``reason``.
                self._fire_closed(close_reason, error_detail=self._last_connect_error)
                if close_reason == TerminateReason.SUPERSEDED.value:
                    _LOGGER.info(
                        "peer-link client to %s:%d superseded by another instance "
                        "with the same dashboard_id; orphaning",
                        self._hostname,
                        self._port,
                    )
                    self._orphaned = True
                    return
                if close_reason == _LOCAL_CLOSE_PIN_MISMATCH:
                    # Pin drift means we're either talking to a
                    # rotated-but-legitimate receiver or to an
                    # attacker; in both cases reconnecting just
                    # hammers the wrong endpoint. The bus event
                    # ``OFFLOADER_PAIR_PIN_MISMATCH`` already
                    # fired from ``_run_one_session`` carries the
                    # diagnostic payload, and the controller's
                    # listener has populated the alerts dict so
                    # the operator sees the warning. Resolution is
                    # user-driven: re-pair (clears the alert) or
                    # unpair (drops the row).
                    _LOGGER.warning(
                        "peer-link client to %s:%d observed pin drift; orphaning "
                        "until the operator re-pairs or unpairs",
                        self._hostname,
                        self._port,
                    )
                    self._orphaned = True
                    return
                # Reset backoff after a session that actually
                # reached ``intent_response: ok`` so a flaky path
                # doesn't permanently degrade to the cap. If we
                # never got past the handshake (transport error,
                # auth rejected, Noise failure), advance the
                # backoff exponentially — a broken receiver
                # mustn't be hammered every second.
                if self._session_was_opened:
                    backoff = _RECONNECT_INITIAL_BACKOFF_SECONDS
                else:
                    backoff = min(backoff * 2, _RECONNECT_MAX_BACKOFF_SECONDS)
                await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            # ``_run_one_session`` already sent the structured
            # ``terminate`` frame in its own CancelledError
            # handler (where the WS and Noise session are still
            # live as locals). All we need to do here is fire
            # the bus event so subscribers see the transition.
            # Even a cancellation before the first session
            # opened benefits from firing this — the controller
            # subscribed to ``OFFLOADER_PEER_LINK_CLOSED`` would
            # otherwise have to track "did this client ever
            # open" itself; the no-OPENED-then-CLOSED sequence
            # is a no-op for any subscriber that keys off
            # OPENED first.
            self._fire_closed(_LOCAL_CLOSE_CLIENT_STOPPED)
            raise

    async def _run_one_session(self) -> str:
        """Run one connect → handshake → receive loop iteration.

        Returns the close reason to propagate into
        ``OFFLOADER_PEER_LINK_CLOSED``. Always returns —
        exceptions are caught and mapped onto a local close
        reason. ``CancelledError`` is the one exception that
        propagates (the run loop's outer handler sends the
        terminate frame).
        """
        self._session_was_opened = False
        url = URL.build(scheme="ws", host=self._hostname, port=self._port, path=PEER_LINK_PATH)
        # ``total`` deliberately omitted: the peer-link session
        # is long-lived (idle-by-design once parked on the
        # receive loop), so a session-wide timeout would forcibly
        # drop a healthy session after ``_DEFAULT_TIMEOUT_SECONDS``.
        # Bound the *handshake* reads with ``asyncio.wait_for``
        # below — that's what the receiver does in
        # ``remote_build_peer_link._HANDSHAKE_READ_TIMEOUT_SECONDS``
        # — so a stalled handshake still fails fast without
        # putting a ceiling on the dispatch loop's lifetime.
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=_DEFAULT_TIMEOUT_SECONDS)
        try:
            async with (
                make_peer_link_http_session(timeout=timeout, resolver=self._resolver) as http,
                http.ws_connect(url, max_msg_size=APP_FRAME_MAX_BYTES) as ws,
            ):
                session = PeerLinkNoiseSession.initiator(self._identity_priv)
                msg3_payload = _json.dumps({"dashboard_id": self._dashboard_id})
                response_ct = await _drive_initiator_handshake_and_read_response(
                    ws=ws,
                    sess=session,
                    intent=PeerLinkIntent.PEER_LINK,
                    msg3_payload=msg3_payload,
                    read_timeout_seconds=_DEFAULT_TIMEOUT_SECONDS,
                )
                # Pin-check the receiver's static pubkey BEFORE
                # decrypting / acting on the response. Noise XX
                # authenticates that the responder holds the
                # private key matching the pubkey it advertised,
                # so a mismatched pubkey here means we connected
                # to a different identity than the one we
                # OOB-confirmed at pair time. Could be a
                # legitimate receiver-side rotation or a MITM /
                # mDNS spoof; either way we abort before any
                # application frames flow.
                if session.remote_static_pub != self._pinned_static_x25519_pub:
                    self._fire_pin_mismatch(observed=session.remote_static_pub)
                    self._last_connect_error = "pin mismatch"
                    return _LOCAL_CLOSE_PIN_MISMATCH
                response = _json.loads(session.decrypt(response_ct))
                if (
                    not isinstance(response, dict)
                    or response.get("intent_response") != IntentResponse.OK.value
                ):
                    _LOGGER.warning(
                        "peer-link client to %s:%d rejected at handshake: %r",
                        self._hostname,
                        self._port,
                        response,
                    )
                    self._last_connect_error = "auth rejected"
                    return _LOCAL_CLOSE_AUTH_REJECTED
                # Lift the receiver's ``esphome_version`` off the
                # response so OPENED carries it onto the bus.
                receiver_version = _extract_receiver_esphome_version(response)
                # Session is live — build the shared channel
                # over (noise, ws), fire OPENED, park on the
                # receive loop with a heartbeat task running
                # alongside. Setting ``_session_was_opened``
                # tells :meth:`run`'s backoff logic to reset on
                # the next iteration. Clearing
                # ``_last_connect_error`` here means a successful
                # reconnect drops the previous failure message
                # off the operator-facing snapshot — a stale "the
                # last connect tried 4 attempts ago failed with
                # ConnectionRefusedError" would mislead the
                # operator into thinking the live session is
                # broken.
                channel = PeerLinkChannel(
                    noise=session, ws=ws, log_label=f"{self._hostname}:{self._port}"
                )
                self._session_was_opened = True
                self._last_connect_error = ""
                self._fire_opened(esphome_version=receiver_version)
                try:
                    return await self._run_session_loops(channel)
                except asyncio.CancelledError:
                    # Best-effort structured close before the
                    # WS goes away under us. The channel's
                    # ``send_terminate`` doesn't go through any
                    # ``_closing`` gate (this terminate IS the
                    # close), so the frame goes out reliably.
                    await channel.send_terminate(_LOCAL_CLOSE_CLIENT_STOPPED)
                    raise
        except (TimeoutError, aiohttp.ClientError, OSError, ValueError, TypeError) as exc:
            _LOGGER.debug(
                "peer-link client to %s:%d transport error: %s",
                self._hostname,
                self._port,
                exc,
                exc_info=True,
            )
            self._last_connect_error = f"{type(exc).__name__}: {exc}"
            return _LOCAL_CLOSE_TRANSPORT_ERROR
        except NOISE_ERRORS as exc:
            _LOGGER.warning(
                "peer-link client to %s:%d Noise failure: %s",
                self._hostname,
                self._port,
                exc,
                exc_info=True,
            )
            self._last_connect_error = f"{type(exc).__name__}: {exc}"
            return _LOCAL_CLOSE_TRANSPORT_ERROR

    async def _run_session_loops(self, channel: PeerLinkChannel) -> str:
        """Run the receive loop with a heartbeat task in parallel.

        Returns the close reason. Both loops mutate a shared
        :class:`_SessionLoopState`: the receive loop bumps
        ``last_pong_at`` on each pong and writes
        ``close_reason`` on transport-error / terminate-frame
        / unknown-msg-type exits; the heartbeat task's
        ``_on_dead`` callback writes
        ``HEARTBEAT_TIMEOUT`` so the close reason reflects the
        real cause instead of falling through to the default
        ``peer_hung_up``. Both loops share the
        :class:`PeerLinkChannel` for encrypt / parse / send.
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
                "peer-link client to %s:%d heartbeat timeout; closing",
                self._hostname,
                self._port,
            )
            # Best-effort close — include ``aiohttp.ClientError``
            # alongside the basic transport types because
            # :meth:`aiohttp.ClientWebSocketResponse.close` can
            # raise ``ClientConnectionError`` / ``ClientError``
            # when the peer has already gone away. Letting that
            # escape here would crash the heartbeat task and let
            # the receive loop fall through to its
            # ``peer_hung_up`` default, masking the real
            # heartbeat-timeout cause. ``CancelledError`` stays
            # unsuppressed (Python 3.8+ excludes it from
            # ``Exception``).
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
        # duration of the receive loop. Cleared in ``finally``
        # so a post-session :meth:`submit_job` raises
        # :class:`PeerLinkNoSessionError` instead of writing
        # into a stale channel.
        self._active_channel = channel
        # Bound the synchronous-dispatch lookup table once per
        # session — sync handlers fan an inbound frame into the
        # bus / ack futures with no need for the channel itself.
        # PING / PONG / TERMINATE / malformed each touch the
        # session loop's mutable state (close_reason,
        # last_pong_at) or the channel (PONG response), so they
        # stay branched in the loop body rather than fitting
        # the table's ``(self, parsed) -> None`` shape.
        sync_dispatch = self._build_sync_frame_dispatch()
        try:
            async for msg in channel.ws:
                parsed = channel.parse_frame(msg)
                if parsed is None:
                    # Any of the four malformed-frame branches —
                    # ``parse_frame`` already logged the per-branch
                    # context. Map to the offloader-side
                    # transport-error reason on the wire-status event.
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
            # Drain any in-flight :meth:`submit_job` callers so
            # they raise :class:`SubmitJobSessionLostError`
            # immediately instead of waiting on the per-flow
            # timeout. The session ended before the ack came
            # back; no point keeping the awaiter parked. Snapshot
            # the dict before iterating because
            # :meth:`submit_job`'s ``finally`` pops the entry as
            # soon as the future fires.
            for pending_job_id, pending_fut in list(self._submit_job_acks.items()):
                if not pending_fut.done():
                    pending_fut.set_exception(
                        SubmitJobSessionLostError(
                            f"submit_job: peer-link session to "
                            f"{self._hostname}:{self._port} ended before ack "
                            f"for job_id={pending_job_id!r}"
                        )
                    )
            # Same drain shape for 6a in-flight artifact downloads —
            # the receiver won't be sending any more chunks now
            # that the session's gone; resolve every pending
            # future so :meth:`download_artifacts` unwinds.
            for pending_job_id, dl_state in list(self._artifacts_downloads.items()):
                if not dl_state.future.done():
                    dl_state.future.set_exception(
                        SubmitJobSessionLostError(
                            f"download_artifacts: peer-link session to "
                            f"{self._hostname}:{self._port} ended before "
                            f"artifacts_end for job_id={pending_job_id!r}"
                        )
                    )
            heartbeat_task.cancel()
            # Drain via ``gather(return_exceptions=True)`` rather
            # than ``suppress(CancelledError) + await`` — suppressing
            # CancelledError swallows any outer cancellation that
            # arrives during the drain and breaks the propagation
            # contract (see ``feedback_no_suppress_cancelled_error``).
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    def _build_sync_frame_dispatch(
        self,
    ) -> dict[str, Callable[[dict[str, Any]], None]]:
        """Return the inbound-frame → sync handler map for one session.

        Built once per session (in :meth:`_run_session_loops`)
        rather than per inbound frame to keep the receive-loop
        hot path's per-frame work down to one dict lookup. The
        bound-method values capture ``self`` so adding a new
        sync frame type is a one-line table entry plus the
        handler implementation, no loop-body branch.

        Excluded from the table on purpose:
        ``PING`` / ``PONG`` / ``TERMINATE`` mutate the
        session-local :class:`_SessionLoopState` or close the
        loop, neither of which fits the ``(parsed)`` shape.
        Malformed frames (``parse_frame`` returned ``None``) are
        a separate branch upstream of this lookup.
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

    def _fire_opened(self, *, esphome_version: str = "") -> None:
        _dispatch.fire_opened(self, esphome_version=esphome_version)

    def _fire_closed(self, reason: str, *, error_detail: str = "") -> None:
        _dispatch.fire_closed(self, reason, error_detail=error_detail)

    def _fire_pin_mismatch(self, *, observed: bytes) -> None:
        _dispatch.fire_pin_mismatch(self, observed=observed)

    def _fire_queue_status(self, idle: bool, running: bool, queue_depth: int) -> None:
        _dispatch.fire_queue_status(self, idle, running, queue_depth)
