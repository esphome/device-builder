"""Tests for the ESPHome Desktop update-integration controller."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.desktop import DesktopController
from esphome_device_builder.helpers.api import CommandError
from esphome_device_builder.helpers.subprocess import CapturedSubprocess
from esphome_device_builder.models import ErrorCode

_BIN = "/Applications/ESPHome Device Builder.app/Contents/MacOS/esphome-desktop"


def _controller(desktop_bin: str = _BIN) -> DesktopController:
    db = MagicMock()
    db.settings.desktop_bin = desktop_bin
    return DesktopController(db)


def _patch_capture(monkeypatch: pytest.MonkeyPatch, captured: CapturedSubprocess) -> AsyncMock:
    run = AsyncMock(return_value=captured)
    monkeypatch.setattr("esphome_device_builder.controllers.desktop.run_subprocess_capture", run)
    return run


async def test_check_update_returns_parsed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "any_available": True,
        "app": {"available": True, "installed": "0.14.0", "latest": "0.15.0"},
        "esphome": {"available": False},
        "device_builder": {"available": False, "installed": None},
    }
    run = _patch_capture(
        monkeypatch,
        CapturedSubprocess(
            returncode=0, stdout=(json.dumps(payload) + "\n").encode(), timed_out=False
        ),
    )

    result = await _controller().check_update()

    assert result == payload
    run.assert_awaited_once()
    assert run.await_args.args == (_BIN, "api", "check-update")
    # stderr must be discarded so stdout carries only the JSON line.
    assert run.await_args.kwargs["merge_stderr"] is False


async def test_check_update_without_desktop_bin_errors() -> None:
    with pytest.raises(CommandError) as exc:
        await _controller(desktop_bin="").check_update()
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


async def test_check_update_nonzero_exit_surfaces_cli_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The CLI prints its error as a JSON line on stdout even on a non-zero
    # exit; the message should reach the CommandError, not just the exit code.
    _patch_capture(
        monkeypatch,
        CapturedSubprocess(
            returncode=3,
            stdout=b'{"type":"err","code":"not_running","message":"the app is not running"}\n',
            timed_out=False,
        ),
    )
    with pytest.raises(CommandError) as exc:
        await _controller().check_update()
    assert "the app is not running" in exc.value.message


async def test_check_update_bin_spawn_oserror_is_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stale/removed ESPHOME_DESKTOP_BIN makes the spawn raise; it must become
    # a CommandError, not an INTERNAL_ERROR traceback.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.desktop.run_subprocess_capture",
        AsyncMock(side_effect=FileNotFoundError("no such file")),
    )
    with pytest.raises(CommandError) as exc:
        await _controller().check_update()
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


async def test_check_update_timeout_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(
        monkeypatch,
        CapturedSubprocess(returncode=None, stdout=b"", timed_out=True),
    )
    with pytest.raises(CommandError):
        await _controller().check_update()


async def test_check_update_unparseable_output_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_capture(
        monkeypatch,
        CapturedSubprocess(returncode=0, stdout=b"not json\n", timed_out=False),
    )
    with pytest.raises(CommandError):
        await _controller().check_update()


async def test_check_update_non_object_payload_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Valid JSON that isn't an object (array/scalar) is rejected.
    _patch_capture(
        monkeypatch,
        CapturedSubprocess(returncode=0, stdout=b"[1, 2, 3]\n", timed_out=False),
    )
    with pytest.raises(CommandError):
        await _controller().check_update()


async def test_check_update_nonzero_exit_without_message_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-zero exit whose JSON line carries no `message`.
    _patch_capture(
        monkeypatch,
        CapturedSubprocess(returncode=1, stdout=b'{"type":"err"}\n', timed_out=False),
    )
    with pytest.raises(CommandError) as exc:
        await _controller().check_update()
    assert "unrecognized error output" in exc.value.message


async def test_check_update_nonzero_exit_without_output_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Non-zero exit with no output at all.
    _patch_capture(
        monkeypatch,
        CapturedSubprocess(returncode=1, stdout=b"", timed_out=False),
    )
    with pytest.raises(CommandError) as exc:
        await _controller().check_update()
    assert "no diagnostic output" in exc.value.message


async def test_update_spawns_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    exec_mock = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(
        "esphome_device_builder.controllers.desktop.create_subprocess_exec", exec_mock
    )

    result = await _controller().update()

    assert result == {"started": True}
    exec_mock.assert_awaited_once()
    assert exec_mock.await_args.args == (_BIN, "api", "update")
    # Own session so a backend restart during install doesn't kill the updater.
    assert exec_mock.await_args.kwargs["start_new_session"] is True


async def test_update_bin_spawn_oserror_is_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "esphome_device_builder.controllers.desktop.create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError("no such file")),
    )
    with pytest.raises(CommandError) as exc:
        await _controller().update()
    assert exc.value.code == ErrorCode.INTERNAL_ERROR


async def test_update_without_desktop_bin_errors() -> None:
    with pytest.raises(CommandError):
        await _controller(desktop_bin="").update()
