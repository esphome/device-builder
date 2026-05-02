"""Tests for ``EditorController`` — YAML validator frontend.

The controller fronts a long-running ``esphome vscode --ace``
subprocess per configuration: stdin / stdout JSON-line protocol,
one warm subprocess reused across edits, with the controller
answering ``read_file`` requests so the validator sees the user's
in-memory buffer instead of whatever's on disk.

Coverage targets:

* ``_resolve_file`` — pure helper, but both branches (in-memory
  match vs ``!include`` disk read) need pinning, including the
  syscall-heavy ``Path.resolve`` cases.
* The ``read_file`` round-trip in ``_validate_locked`` — the
  controller must reply on the same line-protocol the subprocess
  expects, and must do the disk read off the event loop so a
  slow ``!include`` doesn't stall the dashboard.
* Subprocess teardown on stop() and on timeout.

Subprocess interaction is exercised via a fake ``Process`` that
plumbs an ``asyncio.StreamReader`` (stdout) and a small ``_Stdin``
stub whose ``write`` appends every byte buffer to a capture list
(so the test can inspect what the controller sent without needing
a full ``StreamWriter`` or a pipe to drain). That keeps the tests
free of an actual ``esphome`` install while still walking the real
``loads / dumps + readline / write`` codepath.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.editor import (
    EditorController,
    _EditorSession,
)
from esphome_device_builder.helpers.json import dumps


def _make_controller(config_dir: Path) -> EditorController:
    """Build an EditorController bypassing __init__ chains.

    Same shape as ``test_archive_device.py`` — attach a mocked
    ``_db.settings`` so the controller's only dependency is the
    config dir on disk.
    """
    controller = EditorController.__new__(EditorController)
    controller._db = MagicMock()
    controller._db.settings.config_dir = config_dir
    controller._sessions = {}
    controller._esphome_cmd = ["esphome"]
    return controller


# ---------------------------------------------------------------------------
# _resolve_file — pure helper, no event loop
# ---------------------------------------------------------------------------


def test_resolve_file_returns_in_memory_content_for_main_path(tmp_path: Path) -> None:
    """When the validator asks for the config we're editing, return the buffer.

    The dashboard sends an in-memory ``content`` string that may
    differ from disk (the user is mid-edit). Resolving the absolute
    path of ``configuration`` against the config dir and matching
    that to the requested path is what tells us "this is the file
    being edited".
    """
    controller = _make_controller(tmp_path)
    main = tmp_path / "kitchen.yaml"
    main.write_text("# stale on-disk\n", encoding="utf-8")

    result = controller._resolve_file(
        str(main), "kitchen.yaml", "esphome:\n  name: kitchen-edited\n"
    )
    assert result == "esphome:\n  name: kitchen-edited\n"


def test_resolve_file_matches_by_basename(tmp_path: Path) -> None:
    """Bare filename match also returns the in-memory content.

    ``esphome vscode`` sometimes asks by filename rather than
    absolute path (e.g. when the validator has cd'd elsewhere); the
    bare-name check keeps the in-memory shortcut working in that
    case.
    """
    controller = _make_controller(tmp_path)
    result = controller._resolve_file(
        "kitchen.yaml", "kitchen.yaml", "esphome:\n  name: in-memory\n"
    )
    assert result == "esphome:\n  name: in-memory\n"


def test_resolve_file_reads_disk_for_include(tmp_path: Path) -> None:
    """An ``!include`` path different from ``configuration`` reads from disk.

    The validator expands ``!include common.yaml`` by asking the
    controller for that path. We don't shadow disk for those — the
    user only edits one file at a time.
    """
    controller = _make_controller(tmp_path)
    include = tmp_path / "common.yaml"
    include.write_text("captive_portal:\n", encoding="utf-8")

    result = controller._resolve_file(str(include), "kitchen.yaml", "esphome:\n  name: kitchen\n")
    assert result == "captive_portal:\n"


def test_resolve_file_returns_empty_on_missing_include(tmp_path: Path) -> None:
    """Missing include → empty string, never a raise.

    The validator's ``read_file`` protocol doesn't have an "error"
    response — it expects a body. Returning ``""`` lets the
    validator surface its own "file not found" error from inside
    the YAML parse instead of crashing the controller's reader
    loop with an unhandled OSError.
    """
    controller = _make_controller(tmp_path)
    missing = tmp_path / "ghost.yaml"

    result = controller._resolve_file(str(missing), "kitchen.yaml", "")
    assert result == ""


# ---------------------------------------------------------------------------
# _validate_locked — round-trip with a fake subprocess
# ---------------------------------------------------------------------------


def _make_fake_proc(
    stdout_lines: list[bytes],
) -> tuple[Any, asyncio.StreamReader, list[bytes]]:
    """Build a fake ``asyncio.subprocess.Process`` that talks JSON-line.

    Returns ``(proc, stdout_reader, stdin_capture)`` so the test can
    feed additional lines mid-flight (for the ``read_file`` round
    trip) and inspect every byte the controller wrote to stdin.
    """
    reader = asyncio.StreamReader()
    for line in stdout_lines:
        reader.feed_data(line)

    stdin_capture: list[bytes] = []

    class _Stdin:
        def write(self, data: bytes) -> None:
            stdin_capture.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        def is_closing(self) -> bool:
            return False

    proc = MagicMock()
    proc.stdin = _Stdin()
    proc.stdout = reader
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc, reader, stdin_capture


@pytest.mark.asyncio
async def test_validate_locked_returns_result_payload(tmp_path: Path) -> None:
    """Happy-path round trip: send validate, receive ``result``.

    Pin the response shape (yaml_errors + validation_errors) so a
    refactor that drops or renames either field breaks the test —
    the dashboard's editor renders both inline.
    """
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, _reader, stdin_capture = _make_fake_proc(
        [
            dumps(
                {
                    "type": "result",
                    "yaml_errors": [{"message": "bad indent"}],
                    "validation_errors": [{"message": "missing platform"}],
                }
            )
            + b"\n",
        ]
    )
    session.proc = proc
    controller._ensure_subprocess = AsyncMock()  # type: ignore[method-assign]

    result = await controller._validate_locked(session, "kitchen.yaml", "esphome:\n")

    assert result == {
        "yaml_errors": [{"message": "bad indent"}],
        "validation_errors": [{"message": "missing platform"}],
    }
    # Validator received the validate request keyed on the configuration.
    assert b'"type":"validate"' in stdin_capture[0]
    assert b'"file":"kitchen.yaml"' in stdin_capture[0]


@pytest.mark.asyncio
async def test_validate_locked_handles_read_file_round_trip(tmp_path: Path) -> None:
    """``read_file`` is answered with the in-memory buffer, then result returns.

    Critical case: the validator pulls the file being edited from us
    (not from disk) so it sees the user's mid-edit state. The
    response goes back on the same JSON-line stream the validator
    is reading.
    """
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, reader, stdin_capture = _make_fake_proc(
        [
            dumps({"type": "read_file", "path": "kitchen.yaml"}) + b"\n",
        ]
    )
    session.proc = proc
    controller._ensure_subprocess = AsyncMock()  # type: ignore[method-assign]

    async def _feed_result_after_response() -> None:
        # Wait for the controller to send its file_response, then
        # feed the final result line so the loop exits. Wrapped
        # in ``asyncio.timeout`` so a regression in
        # ``_validate_locked`` (e.g. it never writes the
        # file_response) fails the test fast instead of hanging
        # CI; ``stdin_capture`` would otherwise stay at length 1
        # forever.
        async with asyncio.timeout(1.0):
            while len(stdin_capture) < 2:
                await asyncio.sleep(0)
        reader.feed_data(
            dumps({"type": "result", "yaml_errors": [], "validation_errors": []}) + b"\n"
        )

    feeder = asyncio.create_task(_feed_result_after_response())
    try:
        result = await asyncio.wait_for(
            controller._validate_locked(session, "kitchen.yaml", "esphome:\n  name: live\n"),
            timeout=2.0,
        )
    finally:
        # Make sure the feeder doesn't outlive the test even if
        # _validate_locked raised before consuming the result line.
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await feeder

    assert result == {"yaml_errors": [], "validation_errors": []}
    # Second write was the file_response carrying the in-memory buffer.
    assert b'"type":"file_response"' in stdin_capture[1]
    assert b"esphome" in stdin_capture[1]


@pytest.mark.asyncio
async def test_validate_locked_raises_when_subprocess_closes_stdout(
    tmp_path: Path,
) -> None:
    """Empty readline → RuntimeError so ``validate_yaml`` can respawn.

    Subprocess crash / EOF mid-protocol leaves the line buffer empty.
    Bubbling a clear RuntimeError lets the public ``validate_yaml``
    catch + ``_terminate_subprocess`` so the next call gets a fresh
    process.
    """
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, reader, _ = _make_fake_proc([])
    reader.feed_eof()
    session.proc = proc
    controller._ensure_subprocess = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="closed stdout"):
        await controller._validate_locked(session, "kitchen.yaml", "")


# ---------------------------------------------------------------------------
# stop() teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_terminates_all_sessions(tmp_path: Path) -> None:
    """``stop()`` walks every session and clears the registry.

    Without this, an app reload (re-instantiating EditorController)
    leaves orphan ``esphome vscode`` subprocesses pinned to the
    previous config dir.
    """
    controller = _make_controller(tmp_path)
    session_a = _EditorSession()
    session_b = _EditorSession()
    controller._sessions = {"a.yaml": session_a, "b.yaml": session_b}
    controller._terminate_subprocess = AsyncMock()  # type: ignore[method-assign]

    await controller.stop()

    assert controller._sessions == {}
    assert controller._terminate_subprocess.await_count == 2
