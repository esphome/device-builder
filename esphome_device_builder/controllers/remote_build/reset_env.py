"""Receiver-side ``reset_build_env`` dispatch: enqueue the full local reset as a tagged job."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from ...helpers.peer_link_frames import frame_schema, is_valid_frame
from ...models import JobType, ResetBuildEnvAckFrameData, ResetBuildEnvFrameData
from .peer_link import TerminateReason

if TYPE_CHECKING:
    from .peer_link import PeerLinkSession
    from .receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)

_RESET_BUILD_ENV_SCHEMA = frame_schema({"job_id": str})

_REASON_BUSY = "busy"
_REASON_INVALID_FRAME = "invalid_frame"
_REASON_NOT_READY = "not_ready"
_REASON_QUEUE_REJECTED = "queue_rejected"


async def handle_reset_build_env(
    controller: ReceiverController, session: PeerLinkSession, frame: dict[str, Any]
) -> None:
    """
    Enqueue the receiver's full build-env reset for the requesting offloader; ack the outcome.

    Acceptance means a tagged ``RESET_BUILD_ENV`` job was enqueued —
    progress and the terminal state ride the JobFanout stream, not this
    ack. Refuses ``busy`` while any job is active or a bundle is mid-upload.
    """
    if not is_valid_frame(_RESET_BUILD_ENV_SCHEMA, frame):
        _LOGGER.warning(
            "peer-link reset_build_env from %s: malformed frame %r",
            session.dashboard_id,
            frame,
        )
        raw_job_id = frame.get("job_id")
        await _send_ack(
            session,
            job_id=raw_job_id if isinstance(raw_job_id, str) else "",
            accepted=False,
            reason=_REASON_INVALID_FRAME,
        )
        await session.terminate(TerminateReason.MALFORMED_FRAME)
        return
    job_id = cast(ResetBuildEnvFrameData, frame)["job_id"]

    firmware = controller._db.firmware
    if firmware is None:
        await _send_ack(session, job_id=job_id, accepted=False, reason=_REASON_NOT_READY)
        return
    if _reset_busy(controller):
        _LOGGER.info(
            "peer-link reset_build_env from %s refused: receiver busy",
            session.dashboard_id,
        )
        await _send_ack(session, job_id=job_id, accepted=False, reason=_REASON_BUSY)
        return

    # A wiped build tree strands every armed OTA on the receiver's own
    # devices, same as the local reset command.
    firmware._disarm_all_queued_updates()
    job = firmware._create_job(
        "",
        JobType.RESET_BUILD_ENV,
        remote_peer=session.dashboard_id,
        remote_peer_label=controller.approved_peer_label(session.dashboard_id),
        remote_job_id=job_id,
    )
    try:
        await firmware._enqueue(job, supersede=False)
    except Exception:
        _LOGGER.exception("peer-link reset_build_env from %s: enqueue failed", session.dashboard_id)
        firmware.state.jobs.pop(job.job_id, None)
        await _send_ack(session, job_id=job_id, accepted=False, reason=_REASON_QUEUE_REJECTED)
        return
    _LOGGER.info(
        "peer-link reset_build_env from %s: enqueued job %s",
        session.dashboard_id,
        job.job_id,
    )
    await _send_ack(session, job_id=job_id, accepted=True)


def _reset_busy(controller: ReceiverController) -> bool:
    """Whether any job is active (any source / lane) or a bundle is mid-upload."""
    firmware = controller._db.firmware
    if firmware is not None and next(firmware.state.active_jobs(), None) is not None:
        return True
    receiver = controller.state.submit_job_receiver
    return receiver is not None and receiver.has_any_inflight()


async def _send_ack(
    session: PeerLinkSession, *, job_id: str, accepted: bool, reason: str | None = None
) -> None:
    """Send one ``reset_build_env_ack``; warn if delivery is dropped (session closing)."""
    payload: ResetBuildEnvAckFrameData = {
        "type": "reset_build_env_ack",
        "job_id": job_id,
        "accepted": accepted,
    }
    if reason is not None:
        payload["reason"] = reason
    if not await session.send_app_frame(dict(payload)):
        _LOGGER.warning(
            "peer-link reset_build_env_ack (accepted=%s) to %s not delivered — session closing",
            accepted,
            session.dashboard_id,
        )
