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
)
from ...helpers.version_compat import is_pinnable_version
from ...models import JobStatus, ResetBuildEnvAckFrameData
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
    Wipe the requesting offloader's build subtree + cached venv; ack the result.

    Target derives from the session's authenticated ``dashboard_id``; the
    frame's version selects the cached venv. Refuses ``busy`` while that
    offloader has work in flight or another offloader is mid-build on the
    venv. Concurrency contract in ARCHITECTURE.md (Remote build-env reset).
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
    # Version of the venv the offloader's builds use, or None when it
    # matches ours / isn't pinnable (no venv is ever cached for those).
    # Pure check — no filesystem access on the event loop.
    venv_version = _venv_version(cast(str, frame["esphome_version"]))

    if _reset_busy(controller, session.dashboard_id, venv_version=venv_version):
        _LOGGER.info(
            "peer-link reset_build_env from %s refused: jobs in flight",
            session.dashboard_id,
        )
        await _send_ack(session, request_id=request_id, accepted=False, reason=_REASON_BUSY)
        return

    # Wipe the venv first, under its per-version lock, before the long
    # subtree ``rmtree``: a same-version compile arriving during the reset
    # then blocks on the lock and re-provisions clean rather than racing a
    # deletion mid-build. ``CORE.data_dir`` stats ``config_dir`` on access,
    # so the subtree paths are built inside the executor, not on the loop.
    config_dir = Path(controller._db.settings.config_dir)
    # ``wiped`` is pre-seeded so the failure log can report partial
    # progress: the venv wipe runs first, so a subtree failure means the
    # venv is already gone (a retry finishes the subtree — the wipe is
    # idempotent). Any wipe failure must still ack, or the offloader hangs
    # until its 60s ack timeout instead of learning io_error promptly.
    wiped: list[Path] = []
    try:
        wiped = await _wipe_venv(controller, venv_version)
        wiped += await run_in_executor(_wipe_build_env, config_dir, session.dashboard_id)
    except Exception:
        _LOGGER.exception(
            "peer-link reset_build_env from %s failed after wiping %s",
            session.dashboard_id,
            ", ".join(str(t) for t in wiped) or "nothing",
        )
        await _send_ack(session, request_id=request_id, accepted=False, reason=_REASON_IO_ERROR)
        return
    _LOGGER.info(
        "peer-link reset_build_env from %s: wiped %s",
        session.dashboard_id,
        ", ".join(str(t) for t in wiped),
    )
    await _send_ack(session, request_id=request_id, accepted=True)


def _venv_version(version: str) -> str | None:
    """
    Return the version whose cached venv the reset should clear, or ``None``.

    Only a pinnable version that differs from the receiver's installed
    esphome ever gets a provisioned venv; a matching or dev version has
    none. Pure — no filesystem access (safe on the event loop).
    """
    if not version or version == _installed_esphome_version or not is_pinnable_version(version):
        return None
    return version


def _reset_busy(
    controller: ReceiverController, dashboard_id: str, *, venv_version: str | None
) -> bool:
    """
    Whether the reset must be refused ``busy``.

    True when this offloader has any active job (queued / running) or a
    bundle mid-upload, or when another offloader is *running* a build on
    the venv the wipe would clear (``venv_version`` set) — yanking it
    mid-compile truncates that build's toolchain. A merely-queued
    same-version job doesn't block: it re-provisions clean after the
    wipe, and blocking it would strand the last-resort reset behind a
    queue.
    """
    firmware = controller._db.firmware
    if firmware is not None:
        active = list(firmware.active_remote_peer_jobs())
        if any(job.remote_peer == dashboard_id for job in active):
            return True
        if venv_version is not None and any(
            job.status == JobStatus.RUNNING and job.target_esphome_version == venv_version
            for job in active
        ):
            return True
    receiver = controller.state.submit_job_receiver
    return receiver is not None and receiver.has_inflight(dashboard_id)


async def _wipe_venv(controller: ReceiverController, venv_version: str | None) -> list[Path]:
    """
    Wipe the offloader's cached ``esphome-<version>`` venv through the provisioner.

    Delegating to :meth:`EnvProvisioner.reset_version` holds the per-version
    lock, so a concurrent same-version provision can't race the ``rmtree``.
    Empty when there's no venv to clear (version matches the receiver's own /
    isn't pinnable) or the provisioner isn't up.
    """
    if venv_version is None:
        return []
    provisioner = controller.state.env_provisioner
    if provisioner is None:
        return []
    venv = await provisioner.reset_version(venv_version)
    return [venv] if venv is not None else []


def _wipe_build_env(config_dir: Path, dashboard_id: str) -> list[Path]:
    """
    Blocking wipe of the offloader's per-offloader subtrees; return the wiped paths.

    Runs entirely in an executor because building the targets reads
    ``CORE.data_dir`` (which stats). ``dashboard_id`` is already
    ``DASHBOARD_ID_PATTERN``-validated, so the defense-in-depth guard is
    against symlink games on disk: each resolved target must stay within its
    resolved trusted base (the receiver's own config / data dir), which
    rejects any symlink in the ``.remote_builds`` chain that escapes the tree
    — a symlinked root resolves consistently with its own parent and would
    slip a bare parent check.
    """
    data_dir = Path(CORE.data_dir)
    targets = [
        (config_dir, dashboard_config_subtree(config_dir, dashboard_id)),
        (data_dir, dashboard_data_subtree(data_dir, dashboard_id)),
    ]
    for base, target in targets:
        resolved = target.resolve()
        if not resolved.is_relative_to(base.resolve()):
            msg = f"reset_build_env target {target} escapes {base}"
            raise OSError(msg)
        if resolved.exists():
            rmtree(resolved)
    return [target for _, target in targets]


async def _send_ack(
    session: PeerLinkSession, *, request_id: str, accepted: bool, reason: str | None = None
) -> None:
    """Send one ``reset_build_env_ack``; warn if delivery is dropped (session closing)."""
    payload: ResetBuildEnvAckFrameData = {
        "type": "reset_build_env_ack",
        "request_id": request_id,
        "accepted": accepted,
    }
    if reason is not None:
        payload["reason"] = reason
    if not await session.send_app_frame(dict(payload)):
        _LOGGER.warning(
            "peer-link reset_build_env_ack (accepted=%s) to %s not delivered — session closing; "
            "the offloader will fall back to its ack timeout",
            accepted,
            session.dashboard_id,
        )
