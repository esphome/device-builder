"""Receiver-side ``reset_build_env`` dispatch: wipe one offloader's subtree + venv."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from esphome.const import __version__ as _installed_esphome_version
from esphome.core import CORE
from esphome.helpers import rmtree

from ...helpers.async_ import run_in_executor
from ...helpers.peer_link_frames import frame_schema, is_valid_frame
from ...helpers.remote_build_layout import (
    dashboard_config_subtree,
    dashboard_data_subtree,
    venv_dir,
)
from ...helpers.version_compat import is_pinnable_version
from ...models import ResetBuildEnvAckFrameData
from .peer_link import TerminateReason

if TYPE_CHECKING:
    from .peer_link import PeerLinkSession
    from .receiver import ReceiverController

_LOGGER = logging.getLogger(__name__)

_RESET_BUILD_ENV_SCHEMA = frame_schema({"request_id": str, "esphome_version": str})

_REASON_BUSY = "busy"
_REASON_IO_ERROR = "io_error"
_REASON_INVALID_FRAME = "invalid_frame"


async def handle_reset_build_env(
    controller: ReceiverController, session: PeerLinkSession, frame: dict[str, Any]
) -> None:
    """
    Wipe the requesting offloader's build environment; ack the result.

    The target dirs derive exclusively from the session's Noise-
    authenticated ``dashboard_id``; the frame's ``esphome_version`` names
    the offloader's own version so the cached ``esphome-<version>`` venv
    its builds provision is cleared too — the last-resort hammer has to
    reach the shared toolchain, which a subtree wipe alone can't. Refuses
    ``busy`` while this offloader has a job queued / running or a bundle
    mid-upload, or while *any* offloader is compiling with that same
    venv (wiping it mid-build would truncate their toolchain). The
    receive loop awaits this handler, so no same-session submit can
    interleave with the wipe.
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
    # The venv the offloader's builds use, or None when its version
    # matches ours / isn't pinnable (no venv is ever cached for those).
    venv = _venv_for_version(cast(str, frame["esphome_version"]))

    if _reset_busy(controller, session.dashboard_id, venv_version=venv[1] if venv else None):
        _LOGGER.info(
            "peer-link reset_build_env from %s refused: jobs in flight",
            session.dashboard_id,
        )
        await _send_ack(session, request_id=request_id, accepted=False, reason=_REASON_BUSY)
        return

    targets = [
        dashboard_config_subtree(Path(controller._db.settings.config_dir), session.dashboard_id),
        dashboard_data_subtree(Path(CORE.data_dir), session.dashboard_id),
    ]
    if venv is not None:
        targets.append(venv[0])
    try:
        await run_in_executor(_wipe_paths, targets)
    except OSError as exc:
        _LOGGER.warning(
            "peer-link reset_build_env from %s failed: %s",
            session.dashboard_id,
            exc,
        )
        await _send_ack(session, request_id=request_id, accepted=False, reason=_REASON_IO_ERROR)
        return
    _LOGGER.info(
        "peer-link reset_build_env from %s: wiped %s",
        session.dashboard_id,
        ", ".join(str(t) for t in targets),
    )
    await _send_ack(session, request_id=request_id, accepted=True)


def _venv_for_version(version: str) -> tuple[Path, str] | None:
    """
    Return ``(venv_path, version)`` for the offloader's cached venv, or ``None``.

    Only a pinnable version that differs from the receiver's installed
    esphome ever gets a provisioned venv; a matching or dev version has
    none, so there's nothing to wipe.
    """
    if not version or version == _installed_esphome_version or not is_pinnable_version(version):
        return None
    return venv_dir(Path(CORE.data_dir), version), version


def _reset_busy(
    controller: ReceiverController, dashboard_id: str, *, venv_version: str | None
) -> bool:
    """
    Whether the reset must be refused ``busy``.

    True when this offloader has a job queued / running or a bundle
    mid-upload, or when *any* offloader is compiling with the venv the
    wipe would clear (``venv_version`` set) — yanking it mid-build would
    truncate that build's toolchain.
    """
    firmware = controller._db.firmware
    if firmware is not None:
        active = list(firmware.active_remote_peer_jobs())
        if any(job.remote_peer == dashboard_id for job in active):
            return True
        if venv_version is not None and any(
            job.target_esphome_version == venv_version for job in active
        ):
            return True
    receiver = controller.state.submit_job_receiver
    return receiver is not None and receiver.has_inflight(dashboard_id)


def _wipe_paths(targets: list[Path]) -> None:
    """
    Blocking wipe of each per-offloader / venv tree (executor-side).

    Each target's parent is its ``.remote_builds`` root by construction
    (``dashboard_*_subtree`` / ``venv_dir``), so a resolve-under-parent
    check is the defense-in-depth symlink guard — ``dashboard_id`` is
    already ``DASHBOARD_ID_PATTERN``-validated and the version is
    ``is_pinnable_version``-gated, so a failure here means symlink games
    on disk, not wire input.
    """
    for target in targets:
        root = target.parent
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
