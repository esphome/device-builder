"""Receiver-side peer-link session lifecycle + queue-status broadcast."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from ...helpers.api import CommandError
from ...helpers.event_bus import Event
from ...helpers.peer_link_frames import frame_schema, is_valid_frame
from ...models import (
    EventType,
    QueueStatusFrameData,
    ReceiverPeerLinkSessionClosedData,
    ReceiverPeerLinkSessionOpenedData,
)
from .peer_link import PeerLinkSession, TerminateReason

if TYPE_CHECKING:
    from .receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)


# Required fields on inbound ``cancel_job`` peer-link frames.
_CANCEL_JOB_SCHEMA = frame_schema({"job_id": str})


def on_firmware_queue_transition(controller: ReceiverController, event: Event[Any]) -> None:
    """Bus listener: broadcast ``queue_status`` to paired offloaders.

    Called on every ``JOB_QUEUED`` / ``JOB_STARTED`` /
    terminal event. Builds a snapshot from the firmware
    controller's RAM state (sync read, no awaitables in the
    bus listener) and schedules a per-session broadcast as a
    background task. The broadcast itself runs async because
    it sends across N peer-link sessions and we don't want a
    slow socket on one session to block other listeners
    observing the same event.
    """
    if controller._db.firmware is None:
        return
    idle, running, queue_depth = controller._db.firmware.compile_queue_status()
    if not controller.state.peer_link_sessions:
        return
    controller._db.create_background_task(
        broadcast_queue_status(controller, idle=idle, running=running, queue_depth=queue_depth)
    )


async def broadcast_queue_status(
    controller: ReceiverController, *, idle: bool, running: bool, queue_depth: int
) -> None:
    """Send a ``queue_status`` frame to every active peer-link session.

    Snapshot the registry to a list before iterating so a
    concurrent register / unregister mid-walk doesn't mutate
    the dict under us. Each ``send_app_frame`` is gated on
    the session's ``_closing`` flag, so a ``terminate``-in-
    progress session no-ops cleanly here without raising.
    Per-session failures (``send_app_frame`` returns
    ``False``) are logged at the channel layer; we don't
    retry — a session that can't accept the latest snapshot
    will pick up the next transition's broadcast on its
    next successful frame.
    """
    sessions = list(controller.state.peer_link_sessions.values())
    payload = QueueStatusFrameData(
        type="queue_status",
        idle=idle,
        running=running,
        queue_depth=queue_depth,
    )
    for session in sessions:
        # Per-session try/except so one flaky peer can't
        # starve broadcasts to its siblings; the next queue
        # transition will retry.
        try:
            await session.send_app_frame(dict(payload))
        except Exception:
            _LOGGER.exception(
                "queue_status broadcast to session %s raised; continuing with siblings",
                session.dashboard_id,
            )


async def register_peer_link_session(
    controller: ReceiverController, session: PeerLinkSession
) -> None:
    """Register *session*; evict a stale same-``dashboard_id`` slot via SUPERSEDED.

    Install runs before the terminate await so concurrent
    dispatches see the freshest entry. Pushes an initial
    ``queue_status`` to the offloader so cold-connected
    pairings get an idle / running signal without waiting
    on the next firmware transition.
    """
    existing = controller.state.peer_link_sessions.get(session.dashboard_id)
    controller.state.peer_link_sessions[session.dashboard_id] = session
    if existing is not None and existing is not session:
        await existing.terminate(TerminateReason.SUPERSEDED)
    if controller._db.firmware is not None:
        try:
            idle, running, queue_depth = controller._db.firmware.compile_queue_status()
        except Exception:
            # Best-effort: the transition-driven broadcast
            # catches up the offloader on the next change.
            _LOGGER.exception(
                "firmware.compile_queue_status() raised on session register; "
                "skipping initial queue_status push to %s",
                session.dashboard_id,
            )
        else:
            controller._db.create_background_task(
                _send_initial_queue_status(
                    session, idle=idle, running=running, queue_depth=queue_depth
                )
            )
    # Fire AFTER the dict insert so subscriber lookups see
    # the just-registered session.
    if controller._db.bus is not None:
        controller._db.bus.fire(
            EventType.RECEIVER_PEER_LINK_SESSION_OPENED,
            ReceiverPeerLinkSessionOpenedData(dashboard_id=session.dashboard_id),
        )


def unregister_peer_link_session(controller: ReceiverController, session: PeerLinkSession) -> None:
    """
    Drop *session* from the active peer-link registry.

    No-op when a different session has taken the slot (the
    :func:`register_peer_link_session` dedupe path replaces
    the entry before the old session's loop unwinds; the old
    loop's ``finally`` calls this and would otherwise evict
    the new entry). Sync because it's just a dict pop — the
    actual WS close + Noise teardown happens in the session
    loop's ``finally`` chain.
    """
    if controller.state.peer_link_sessions.get(session.dashboard_id) is session:
        del controller.state.peer_link_sessions[session.dashboard_id]
        # Drop any in-flight ``submit_job`` upload state so a
        # bundle reception that was mid-stream when the
        # session ended doesn't outlive the session that owns
        # it. ``submit_job_receiver`` is set in
        # :meth:`ReceiverController.start`; this branch only
        # runs for sessions registered after ``start`` (live
        # wire), so the attribute is always populated by the
        # time we get here.
        if controller.state.submit_job_receiver is not None:
            controller.state.submit_job_receiver.discard_session(session.dashboard_id)
        if controller.state.artifacts_download_sender is not None:
            controller.state.artifacts_download_sender.discard_session(session.dashboard_id)
        # Fire only when we actually dropped the slot — the
        # no-op path (a SUPERSEDED-evicted session running its
        # finally-block after the new session has taken its
        # place) would double-fire CLOSED for a single
        # logical close otherwise.
        if controller._db.bus is not None:
            controller._db.bus.fire(
                EventType.RECEIVER_PEER_LINK_SESSION_CLOSED,
                ReceiverPeerLinkSessionClosedData(dashboard_id=session.dashboard_id),
            )


async def handle_cancel_job(
    controller: ReceiverController, session: PeerLinkSession, frame: dict[str, Any]
) -> None:
    """Receiver-side dispatch for inbound ``cancel_job`` frames.

    Resolves the offloader's ``job_id`` to the receiver-local
    :class:`FirmwareJob` via :class:`JobFanout` and routes
    through :meth:`FirmwareController.cancel` — same path as
    an operator-driven cancel. No wire ack; the fan-out's
    ``job_state_changed{cancelled}`` carries the result.

    Silent debug-log drops for malformed frames, unknown
    correlations (race with a terminal transition), and
    :class:`CommandError` from cancel (already-terminal).
    """
    if not is_valid_frame(_CANCEL_JOB_SCHEMA, frame):
        _LOGGER.debug(
            "peer-link cancel_job from %s: malformed frame; dropping: %r",
            session.dashboard_id,
            frame,
        )
        return
    if controller.state.job_fanout is None or controller._db.firmware is None:
        _LOGGER.debug(
            "peer-link cancel_job from %s before controller fully started; dropping",
            session.dashboard_id,
        )
        return
    remote_job_id = cast(str, frame["job_id"])
    firmware_job_id = controller.state.job_fanout.resolve_firmware_job_id(
        session.dashboard_id, remote_job_id
    )
    if firmware_job_id is None:
        _LOGGER.debug(
            "peer-link cancel_job from %s: no firmware job for remote_job_id=%r; dropping",
            session.dashboard_id,
            remote_job_id,
        )
        return
    try:
        await controller._db.firmware.cancel(job_id=firmware_job_id)
    except CommandError as exc:
        _LOGGER.debug(
            "peer-link cancel_job from %s: firmware refused cancel for job %s: %s",
            session.dashboard_id,
            firmware_job_id,
            exc.message,
        )


async def _send_initial_queue_status(
    session: PeerLinkSession,
    *,
    idle: bool,
    running: bool,
    queue_depth: int,
) -> None:
    """Push a one-shot ``queue_status`` frame to a freshly-connected session."""
    payload = QueueStatusFrameData(
        type="queue_status",
        idle=idle,
        running=running,
        queue_depth=queue_depth,
    )
    try:
        await session.send_app_frame(dict(payload))
    except Exception:
        _LOGGER.exception(
            "initial queue_status to session %s raised; "
            "offloader will catch up on the next queue transition",
            session.dashboard_id,
        )
