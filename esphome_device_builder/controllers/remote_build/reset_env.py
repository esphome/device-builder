"""Receiver-side ``reset_build_env`` dispatch: wipe one offloader's subtree."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from esphome.core import CORE
from esphome.helpers import rmtree

from ...helpers.async_ import run_in_executor
from ...helpers.peer_link_frames import frame_schema, is_valid_frame
from ...helpers.remote_build_layout import (
    REMOTE_BUILDS_NAME,
    REMOTE_BUILDS_SUBDIR,
    dashboard_config_subtree,
    dashboard_data_subtree,
)
from ...models import ResetBuildEnvAckFrameData
from .peer_link import TerminateReason

if TYPE_CHECKING:
    from .peer_link import PeerLinkSession
    from .receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)

_RESET_BUILD_ENV_SCHEMA = frame_schema({"request_id": str})

_REASON_BUSY = "busy"
_REASON_IO_ERROR = "io_error"
_REASON_INVALID_FRAME = "invalid_frame"


async def handle_reset_build_env(
    controller: ReceiverController, session: PeerLinkSession, frame: dict[str, Any]
) -> None:
    """
    Wipe the requesting offloader's isolated build subtrees; ack the result.

    The target dirs derive exclusively from the session's Noise-
    authenticated ``dashboard_id`` — the frame carries only a
    ``request_id``. Refuses ``busy`` while the offloader has a job
    queued / running or a bundle mid-upload here; the shared venv
    cache is never touched. The receive loop awaits this handler,
    so no same-session submit can interleave with the wipe.
    """
    if not is_valid_frame(_RESET_BUILD_ENV_SCHEMA, frame):
        _LOGGER.warning(
            "peer-link reset_build_env from %s: malformed frame %r",
            session.dashboard_id,
            frame,
        )
        request_id = frame.get("request_id")
        await _send_ack(
            session,
            request_id=request_id if isinstance(request_id, str) else "",
            accepted=False,
            reason=_REASON_INVALID_FRAME,
        )
        await session.terminate(TerminateReason.MALFORMED_FRAME)
        return
    request_id = cast(str, frame["request_id"])

    if _offloader_busy(controller, session.dashboard_id):
        _LOGGER.info(
            "peer-link reset_build_env from %s refused: jobs in flight",
            session.dashboard_id,
        )
        await _send_ack(session, request_id=request_id, accepted=False, reason=_REASON_BUSY)
        return

    config_subtree = dashboard_config_subtree(
        Path(controller._db.settings.config_dir), session.dashboard_id
    )
    data_subtree = dashboard_data_subtree(Path(CORE.data_dir), session.dashboard_id)
    try:
        await run_in_executor(_wipe_subtrees, config_subtree, data_subtree)
    except OSError as exc:
        _LOGGER.warning(
            "peer-link reset_build_env from %s failed: %s",
            session.dashboard_id,
            exc,
        )
        await _send_ack(session, request_id=request_id, accepted=False, reason=_REASON_IO_ERROR)
        return
    _LOGGER.info(
        "peer-link reset_build_env from %s: wiped %s and %s",
        session.dashboard_id,
        config_subtree,
        data_subtree,
    )
    await _send_ack(session, request_id=request_id, accepted=True)


def _offloader_busy(controller: ReceiverController, dashboard_id: str) -> bool:
    """Whether *dashboard_id* has a job queued / running or a bundle mid-upload."""
    firmware = controller._db.firmware
    if firmware is not None and any(
        job.remote_peer == dashboard_id for job in firmware.active_remote_peer_jobs()
    ):
        return True
    receiver = controller.state.submit_job_receiver
    return receiver is not None and receiver.has_inflight(dashboard_id)


def _wipe_subtrees(config_subtree: Path, data_subtree: Path) -> None:
    """
    Blocking wipe of both per-offloader trees (executor-side).

    Defense-in-depth: each target must still resolve under its
    remote-builds root before the ``rmtree`` — ``dashboard_id`` is
    already ``DASHBOARD_ID_PATTERN``-validated at the handshake, so
    a failure here indicates symlink games on disk, not wire input.
    """
    for target, root_parts in (
        (config_subtree, REMOTE_BUILDS_SUBDIR.parts),
        (data_subtree, (REMOTE_BUILDS_NAME,)),
    ):
        root = target.parent
        if root.parts[-len(root_parts) :] != tuple(root_parts):
            msg = f"reset_build_env target {target} is not under a remote-builds root"
            raise OSError(msg)
        resolved = target.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as exc:
            msg = f"reset_build_env target {target} escapes {root}"
            raise OSError(msg) from exc
        if resolved.exists():
            rmtree(resolved)


async def _send_ack(
    session: PeerLinkSession, *, request_id: str, accepted: bool, reason: str | None = None
) -> None:
    """Send one ``reset_build_env_ack``; best-effort (send failures are logged upstream)."""
    payload: ResetBuildEnvAckFrameData = {
        "type": "reset_build_env_ack",
        "request_id": request_id,
        "accepted": accepted,
    }
    if reason is not None:
        payload["reason"] = reason
    await session.send_app_frame(dict(payload))
