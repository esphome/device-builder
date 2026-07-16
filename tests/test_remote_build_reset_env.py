"""Receiver-side ``reset_build_env`` handler: busy gate, tagged job enqueue, acks."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from esphome_device_builder.controllers.remote_build import reset_env
from esphome_device_builder.controllers.remote_build.peer_link import (
    PeerLinkSession,
    TerminateReason,
)
from esphome_device_builder.models import JobType

from .conftest import RemoteBuildTestHandles, make_remote_build_controller

_DASHBOARD_ID = "abcdef0123456789"


def _frame(job_id: str = "offl-job-1") -> dict[str, Any]:
    return {"type": "reset_build_env", "job_id": job_id}


def _make_session(dashboard_id: str = _DASHBOARD_ID) -> MagicMock:
    session = MagicMock(spec=PeerLinkSession)
    session.dashboard_id = dashboard_id
    session.send_app_frame = AsyncMock(return_value=True)
    session.terminate = AsyncMock()
    return session


def _sent_ack(session: MagicMock) -> dict[str, Any]:
    session.send_app_frame.assert_awaited_once()
    return session.send_app_frame.call_args.args[0]


def _wire_firmware(handles: RemoteBuildTestHandles, *, active_jobs: list[Any]) -> MagicMock:
    """Attach a firmware-controller stub with the surface the handler touches."""
    firmware = MagicMock()
    firmware.state.active_jobs = MagicMock(side_effect=lambda: iter(active_jobs))
    firmware.state.jobs = {}
    firmware._enqueue = AsyncMock(side_effect=lambda job, **_: job)
    created = MagicMock()
    created.job_id = "rcvr-job-1"
    firmware._create_job = MagicMock(return_value=created)
    handles.receiver._db.firmware = firmware
    return firmware


def _make_handles(tmp_path: Path) -> RemoteBuildTestHandles:
    handles = make_remote_build_controller(config_dir=tmp_path)
    handles.receiver.state.submit_job_receiver = None
    return handles


async def test_reset_enqueues_tagged_job_and_acks_accepted(tmp_path: Path) -> None:
    """An idle receiver enqueues a RESET_BUILD_ENV job tagged for the fan-out."""
    handles = _make_handles(tmp_path)
    firmware = _wire_firmware(handles, active_jobs=[])
    peer = MagicMock()
    peer.label = "office-node"
    handles.receiver.state.approved_peers[_DASHBOARD_ID] = peer
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame("offl-1"))

    ack = _sent_ack(session)
    assert ack == {"type": "reset_build_env_ack", "job_id": "offl-1", "accepted": True}
    firmware._disarm_all_queued_updates.assert_called_once()
    firmware._create_job.assert_called_once_with(
        "",
        JobType.RESET_BUILD_ENV,
        remote_peer=_DASHBOARD_ID,
        remote_peer_label="office-node",
        remote_job_id="offl-1",
    )
    firmware._enqueue.assert_awaited_once()
    assert firmware._enqueue.call_args.kwargs == {"supersede": False}
    session.terminate.assert_not_called()


async def test_reset_refused_busy_while_any_job_active(tmp_path: Path) -> None:
    """Any active job — any offloader's, any source — refuses the reset."""
    handles = _make_handles(tmp_path)
    firmware = _wire_firmware(handles, active_jobs=[MagicMock()])
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame())

    ack = _sent_ack(session)
    assert ack["accepted"] is False
    assert ack["reason"] == "busy"
    firmware._create_job.assert_not_called()
    firmware._disarm_all_queued_updates.assert_not_called()


async def test_reset_refused_busy_while_bundle_inflight(tmp_path: Path) -> None:
    """A bundle mid-upload from any offloader refuses the reset."""
    handles = _make_handles(tmp_path)
    firmware = _wire_firmware(handles, active_jobs=[])
    receiver_stub = MagicMock()
    receiver_stub.has_any_inflight = MagicMock(return_value=True)
    handles.receiver.state.submit_job_receiver = receiver_stub
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame())

    ack = _sent_ack(session)
    assert ack["accepted"] is False
    assert ack["reason"] == "busy"
    firmware._create_job.assert_not_called()


async def test_reset_without_firmware_acks_not_ready(tmp_path: Path) -> None:
    """No firmware controller wired: ack ``not_ready`` instead of raising."""
    handles = _make_handles(tmp_path)
    handles.receiver._db.firmware = None
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame())

    ack = _sent_ack(session)
    assert ack["accepted"] is False
    assert ack["reason"] == "not_ready"


async def test_reset_malformed_frame_acks_and_terminates(tmp_path: Path) -> None:
    """A frame missing ``job_id`` acks invalid_frame + terminates the session."""
    handles = _make_handles(tmp_path)
    _wire_firmware(handles, active_jobs=[])
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, {"type": "reset_build_env"})

    ack = _sent_ack(session)
    assert ack["accepted"] is False
    assert ack["reason"] == "invalid_frame"
    session.terminate.assert_awaited_once_with(TerminateReason.MALFORMED_FRAME)


async def test_reset_enqueue_failure_rolls_back_and_acks_queue_rejected(
    tmp_path: Path,
) -> None:
    """An ``_enqueue`` exception pops the orphan job and acks ``queue_rejected``."""
    handles = _make_handles(tmp_path)
    firmware = _wire_firmware(handles, active_jobs=[])
    firmware._enqueue = AsyncMock(side_effect=RuntimeError("queue wedged"))
    created = firmware._create_job.return_value
    firmware.state.jobs[created.job_id] = created
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame())

    ack = _sent_ack(session)
    assert ack["accepted"] is False
    assert ack["reason"] == "queue_rejected"
    assert created.job_id not in firmware.state.jobs


async def test_reset_ack_delivery_failure_is_logged(tmp_path: Path, caplog: Any) -> None:
    """A dropped ack (session closing) is logged, not silently swallowed."""
    handles = _make_handles(tmp_path)
    _wire_firmware(handles, active_jobs=[])
    session = _make_session()
    session.send_app_frame = AsyncMock(return_value=False)

    with caplog.at_level("WARNING"):
        await reset_env.handle_reset_build_env(handles.receiver, session, _frame())

    session.send_app_frame.assert_awaited_once()
    assert "not delivered" in caplog.text
