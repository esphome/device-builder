"""Receiver-side ``reset_build_env`` handler: scoped wipe, venv, busy gate, acks."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from esphome.const import __version__ as _installed_esphome_version
from esphome.core import CORE

from esphome_device_builder.controllers.remote_build import reset_env
from esphome_device_builder.controllers.remote_build.peer_link import (
    PeerLinkSession,
    TerminateReason,
)
from esphome_device_builder.helpers.remote_build_layout import (
    dashboard_config_subtree,
    dashboard_data_subtree,
    venv_dir,
)

from .conftest import RemoteBuildTestHandles, make_remote_build_controller

_DASHBOARD_ID = "abcdef0123456789"
_OTHER_DASHBOARD_ID = "zzzzzzzz11111111"
# A pinnable release that won't collide with the test env's installed
# esphome (date-based), so its venv is always a wipe candidate.
_VERSION = "1.0.0"
_OTHER_VERSION = "2.0.0"


def _frame(request_id: str, *, version: str = _VERSION) -> dict[str, Any]:
    return {"type": "reset_build_env", "request_id": request_id, "esphome_version": version}


def _make_session(dashboard_id: str = _DASHBOARD_ID) -> MagicMock:
    session = MagicMock(spec=PeerLinkSession)
    session.dashboard_id = dashboard_id
    session.send_app_frame = AsyncMock(return_value=True)
    session.terminate = AsyncMock()
    return session


def _sent_ack(session: MagicMock) -> dict[str, Any]:
    session.send_app_frame.assert_awaited_once()
    return session.send_app_frame.call_args.args[0]


def _seed_subtrees(config_dir: Path, dashboard_id: str) -> tuple[Path, Path]:
    """Create both per-offloader trees with a sentinel file each."""
    config_subtree = dashboard_config_subtree(config_dir, dashboard_id)
    data_subtree = dashboard_data_subtree(Path(CORE.data_dir), dashboard_id)
    for base in (config_subtree / "kitchen", data_subtree / ".esphome" / "build"):
        base.mkdir(parents=True, exist_ok=True)
        (base / "sentinel.txt").write_text("x")
    return config_subtree, data_subtree


def _seed_venv(version: str) -> Path:
    venv = venv_dir(Path(CORE.data_dir), version)
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "sentinel.txt").write_text("x")
    return venv


def _remote_job(dashboard_id: str, version: str) -> MagicMock:
    job = MagicMock()
    job.remote_peer = dashboard_id
    job.target_esphome_version = version
    return job


def _wire_firmware(handles: RemoteBuildTestHandles, jobs: list[MagicMock]) -> None:
    firmware = MagicMock()
    firmware.active_remote_peer_jobs = MagicMock(side_effect=lambda: iter(jobs))
    handles.receiver._db.firmware = firmware


def _make_handles(tmp_path: Path) -> RemoteBuildTestHandles:
    handles = make_remote_build_controller(config_dir=tmp_path)
    # No firmware controller / submit receiver by default: not busy.
    handles.receiver._db.firmware = None
    handles.receiver.state.submit_job_receiver = None
    return handles


async def test_reset_wipes_subtrees_and_the_requesters_venv(tmp_path: Path) -> None:
    """Success wipes the requester's trees + its venv; neighbours survive."""
    handles = _make_handles(tmp_path)
    config_subtree, data_subtree = _seed_subtrees(tmp_path, _DASHBOARD_ID)
    other_config, other_data = _seed_subtrees(tmp_path, _OTHER_DASHBOARD_ID)
    my_venv = _seed_venv(_VERSION)
    other_venv = _seed_venv(_OTHER_VERSION)
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame("r1"))

    assert _sent_ack(session)["accepted"] is True
    assert not config_subtree.exists()
    assert not data_subtree.exists()
    assert not my_venv.exists()
    # Other offloader's trees and unrelated version venvs are untouched.
    assert other_config.exists()
    assert other_data.exists()
    assert other_venv.exists()
    session.terminate.assert_not_called()


async def test_reset_leaves_venv_when_version_matches_installed(tmp_path: Path) -> None:
    """A version equal to the receiver's installed esphome has no venv to wipe."""
    handles = _make_handles(tmp_path)
    _seed_subtrees(tmp_path, _DASHBOARD_ID)
    # Even if a venv dir happens to exist for the installed version, the
    # handler must not target it (the provisioner never caches that one).
    venv = _seed_venv(_installed_esphome_version)
    session = _make_session()

    await reset_env.handle_reset_build_env(
        handles.receiver, session, _frame("r2", version=_installed_esphome_version)
    )

    assert _sent_ack(session)["accepted"] is True
    assert venv.exists()


async def test_reset_leaves_venv_for_unpinnable_version(tmp_path: Path) -> None:
    """A dev / non-pinnable version never has a provisioned venv to wipe."""
    handles = _make_handles(tmp_path)
    _seed_subtrees(tmp_path, _DASHBOARD_ID)
    venv = _seed_venv("2026.8.0-dev")
    session = _make_session()

    await reset_env.handle_reset_build_env(
        handles.receiver, session, _frame("r3", version="2026.8.0-dev")
    )

    assert _sent_ack(session)["accepted"] is True
    assert venv.exists()


async def test_reset_with_missing_dirs_still_acks_accepted(tmp_path: Path) -> None:
    """Nothing on disk yet: the wipe is a no-op success, not an error."""
    handles = _make_handles(tmp_path)
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame("r4"))

    assert _sent_ack(session)["accepted"] is True


async def test_reset_refused_busy_while_own_job_active(tmp_path: Path) -> None:
    """A queued/running job from the same offloader refuses the wipe."""
    handles = _make_handles(tmp_path)
    _wire_firmware(handles, [_remote_job(_DASHBOARD_ID, _VERSION)])
    config_subtree, _ = _seed_subtrees(tmp_path, _DASHBOARD_ID)
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame("r5"))

    ack = _sent_ack(session)
    assert ack["accepted"] is False
    assert ack["reason"] == "busy"
    assert config_subtree.exists()


async def test_reset_refused_when_another_offloader_uses_the_venv(tmp_path: Path) -> None:
    """Another offloader compiling with the same venv version refuses the wipe."""
    handles = _make_handles(tmp_path)
    _wire_firmware(handles, [_remote_job(_OTHER_DASHBOARD_ID, _VERSION)])
    my_venv = _seed_venv(_VERSION)
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame("r6"))

    ack = _sent_ack(session)
    assert ack["accepted"] is False
    assert ack["reason"] == "busy"
    assert my_venv.exists()


async def test_reset_proceeds_when_other_offloader_builds_a_different_version(
    tmp_path: Path,
) -> None:
    """Another offloader on a different venv version doesn't block the wipe."""
    handles = _make_handles(tmp_path)
    _wire_firmware(handles, [_remote_job(_OTHER_DASHBOARD_ID, _OTHER_VERSION)])
    my_venv = _seed_venv(_VERSION)
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame("r7"))

    assert _sent_ack(session)["accepted"] is True
    assert not my_venv.exists()


async def test_reset_refused_busy_while_bundle_inflight(tmp_path: Path) -> None:
    """A bundle mid-upload from the same offloader refuses the wipe."""
    handles = _make_handles(tmp_path)
    receiver_stub = MagicMock()
    receiver_stub.has_inflight = MagicMock(return_value=True)
    handles.receiver.state.submit_job_receiver = receiver_stub
    session = _make_session()

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame("r8"))

    ack = _sent_ack(session)
    assert ack["accepted"] is False
    assert ack["reason"] == "busy"
    receiver_stub.has_inflight.assert_called_once_with(_DASHBOARD_ID)


async def test_reset_malformed_frame_acks_and_terminates(tmp_path: Path) -> None:
    """A frame missing a required field acks invalid_frame + terminates the session."""
    handles = _make_handles(tmp_path)
    session = _make_session()

    await reset_env.handle_reset_build_env(
        handles.receiver, session, {"type": "reset_build_env", "request_id": "r9"}
    )

    ack = _sent_ack(session)
    assert ack["accepted"] is False
    assert ack["reason"] == "invalid_frame"
    session.terminate.assert_awaited_once_with(TerminateReason.MALFORMED_FRAME)


async def test_reset_target_derives_from_session_identity(tmp_path: Path) -> None:
    """A hostile dashboard/dir field in the frame is ignored; the session id wins."""
    handles = _make_handles(tmp_path)
    victim_config, victim_data = _seed_subtrees(tmp_path, _OTHER_DASHBOARD_ID)
    own_config, own_data = _seed_subtrees(tmp_path, _DASHBOARD_ID)
    session = _make_session()

    frame = _frame("r10")
    frame["dashboard_id"] = _OTHER_DASHBOARD_ID
    frame["dir"] = "../../.."
    await reset_env.handle_reset_build_env(handles.receiver, session, frame)

    assert _sent_ack(session)["accepted"] is True
    assert victim_config.exists()
    assert victim_data.exists()
    assert not own_config.exists()
    assert not own_data.exists()


async def test_reset_io_error_acks_io_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``OSError`` from the wipe surfaces as an ``io_error`` refusal."""
    handles = _make_handles(tmp_path)
    _seed_subtrees(tmp_path, _DASHBOARD_ID)
    session = _make_session()

    def _boom(*_args: object) -> None:
        raise OSError("disk on fire")

    monkeypatch.setattr(reset_env, "_wipe_paths", _boom)

    await reset_env.handle_reset_build_env(handles.receiver, session, _frame("r11"))

    ack = _sent_ack(session)
    assert ack["accepted"] is False
    assert ack["reason"] == "io_error"
