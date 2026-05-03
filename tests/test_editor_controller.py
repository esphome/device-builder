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


# ---------------------------------------------------------------------------
# __init__ + start
# ---------------------------------------------------------------------------


def test_init_sets_default_state() -> None:
    """Constructor wires the device builder, no sessions, empty cmd."""
    db = MagicMock()
    controller = EditorController(db)
    assert controller._db is db
    assert controller._sessions == {}
    assert controller._esphome_cmd == []


@pytest.mark.asyncio
async def test_start_resolves_esphome_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start()`` populates ``_esphome_cmd`` via ``_find_esphome_cmd``.

    The cmd is later spliced with ``vscode <config_dir> --ace`` to
    spawn the validator. Pin the lookup so a refactor that moved
    the resolution elsewhere (or skipped it) surfaces here rather
    than at the first ``editor/validate_yaml`` call.
    """
    controller = _make_controller(tmp_path)
    controller._esphome_cmd = []  # ensure start() actually populates it

    monkeypatch.setattr(
        "esphome_device_builder.controllers.editor._find_esphome_cmd",
        lambda: ["python", "-m", "esphome"],
    )

    await controller.start()

    assert controller._esphome_cmd == ["python", "-m", "esphome"]


# ---------------------------------------------------------------------------
# _ensure_subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_subprocess_no_op_when_proc_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live proc on the session → no respawn.

    Sessions are warm: a single ``esphome vscode`` subprocess
    serves every validate call for that configuration. A respawn
    would lose the validator's component-import cache and double
    every validation's wall-clock cost.
    """
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, *_ = _make_fake_proc([])
    session.proc = proc
    spawned = False

    async def _no_spawn(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal spawned
        spawned = True
        return MagicMock()

    # Patch the spawn entry point — should never be reached.
    # Use ``monkeypatch.setattr`` so the override is restored after
    # the test; direct module-attribute assignment would leak to
    # subsequent tests and produce order-dependent failures.
    monkeypatch.setattr(
        "esphome_device_builder.controllers.editor.create_subprocess_exec",
        _no_spawn,
    )

    await controller._ensure_subprocess(session)

    assert spawned is False
    assert session.proc is proc


@pytest.mark.asyncio
async def test_ensure_subprocess_spawns_and_drains_version_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cold-start: spawn the subprocess and drain the initial version line.

    Without the drain, the next ``validate`` call would land on the
    stale version line and never reach the real result — pinning
    this branch protects against a refactor that drops the readline.
    """
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, reader, _ = _make_fake_proc([dumps({"type": "version", "version": "1.0"}) + b"\n"])

    async def _fake_spawn(*_args: Any, **_kwargs: Any) -> Any:
        return proc

    monkeypatch.setattr(
        "esphome_device_builder.controllers.editor.create_subprocess_exec",
        _fake_spawn,
    )

    await controller._ensure_subprocess(session)

    assert session.proc is proc
    # The version line should already have been consumed — feed_eof
    # on the reader and a follow-up readline returns nothing.
    reader.feed_eof()
    assert await proc.stdout.readline() == b""


@pytest.mark.asyncio
async def test_ensure_subprocess_terminates_session_and_raises_on_startup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subprocess never emits version line within ``_STARTUP_TIMEOUT``.

    The validator hung before printing its handshake. Tear down the
    session and raise so the dispatcher surfaces a clear error
    instead of leaving a half-initialised subprocess pinned. Verify
    both: ``RuntimeError`` propagates, and ``_terminate_subprocess``
    runs.
    """
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, _reader, _ = _make_fake_proc([])  # no lines → readline blocks

    async def _fake_spawn(*_args: Any, **_kwargs: Any) -> Any:
        return proc

    monkeypatch.setattr(
        "esphome_device_builder.controllers.editor.create_subprocess_exec",
        _fake_spawn,
    )

    async def _raise_timeout(awaitable: Any, *_args: Any, **_kwargs: Any) -> None:
        # Close the awaitable so a "coroutine was never awaited"
        # warning doesn't fire on the never-consumed readline.
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(
        "esphome_device_builder.controllers.editor.asyncio.wait_for",
        _raise_timeout,
    )

    terminated = AsyncMock()
    controller._terminate_subprocess = terminated  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="did not start in time"):
        await controller._ensure_subprocess(session)

    terminated.assert_awaited_once_with(session)


# ---------------------------------------------------------------------------
# _terminate_subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_subprocess_no_op_when_session_proc_is_none(
    tmp_path: Path,
) -> None:
    """No proc → return immediately, no surprise calls."""
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    session.proc = None
    await controller._terminate_subprocess(session)


@pytest.mark.asyncio
async def test_terminate_subprocess_no_op_when_proc_already_exited(
    tmp_path: Path,
) -> None:
    """Already-exited proc skips the exit/terminate/kill ladder."""
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, *_ = _make_fake_proc([])
    proc.returncode = 0
    session.proc = proc

    await controller._terminate_subprocess(session)

    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


@pytest.mark.asyncio
async def test_terminate_subprocess_sends_exit_and_waits(tmp_path: Path) -> None:
    """Happy path: write ``{"type": "exit"}``, drain, close stdin, wait."""
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, _reader, stdin_capture = _make_fake_proc([])
    session.proc = proc

    await controller._terminate_subprocess(session)

    # Exit message went out.
    assert any(b'"type":"exit"' in chunk for chunk in stdin_capture)
    # No escalation needed — proc.wait() returned cleanly.
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()
    # Session's reference cleared so the next ensure() respawns.
    assert session.proc is None


@pytest.mark.asyncio
async def test_terminate_subprocess_escalates_through_terminate_then_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subprocess ignores exit + terminate → final ``kill_quietly`` fires.

    Pin the full ladder. The validator may be wedged in C extension
    code that doesn't honour the ``exit`` message or SIGTERM (rare,
    but the branch exists for it); the SIGKILL fallback is what
    keeps shutdown from hanging.
    """
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, _reader, _ = _make_fake_proc([])
    session.proc = proc

    timeouts = [True, True, False]  # exit-wait, terminate-wait, kill-wait

    async def _wait_for(awaitable: Any, *_args: Any, **_kwargs: Any) -> Any:
        if hasattr(awaitable, "close"):
            awaitable.close()
        if timeouts.pop(0):
            raise TimeoutError
        return 0

    monkeypatch.setattr(
        "esphome_device_builder.controllers.editor.asyncio.wait_for",
        _wait_for,
    )

    kill_quietly_calls: list[Any] = []

    def _track_kill_quietly(p: Any) -> None:
        kill_quietly_calls.append(p)

    monkeypatch.setattr(
        "esphome_device_builder.controllers.editor.kill_quietly",
        _track_kill_quietly,
    )

    await controller._terminate_subprocess(session)

    proc.terminate.assert_called_once()
    assert kill_quietly_calls == [proc]


@pytest.mark.asyncio
async def test_terminate_subprocess_swallows_stdin_write_failure(
    tmp_path: Path,
) -> None:
    """A broken stdin doesn't abort termination — fall through to wait/kill.

    Production trigger: the validator died between the wedge check
    and the exit-message write, so ``proc.stdin.write`` raises
    ``BrokenPipeError``. We need to keep going and still ``wait()``
    so the session reference clears; the ``except Exception`` is
    what makes that work.
    """
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, _reader, _ = _make_fake_proc([])

    def _broken_write(_data: bytes) -> None:
        raise BrokenPipeError

    proc.stdin.write = _broken_write
    session.proc = proc

    await controller._terminate_subprocess(session)

    # Cleared the session reference even after the write failure.
    assert session.proc is None


# ---------------------------------------------------------------------------
# _resolve_file — OSError on Path.resolve
# ---------------------------------------------------------------------------


def test_resolve_file_falls_back_when_requested_path_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Path.resolve()`` raising on the requested path → use the unresolved Path.

    macOS / Linux with a long enough path (or a path through a
    broken symlink) can fault ``resolve()`` even when ``read_text``
    later succeeds against the unresolved form. The except branch
    keeps the read-from-disk fallback usable in that case rather
    than 500-ing the validator round-trip.
    """
    controller = _make_controller(tmp_path)
    include = tmp_path / "common.yaml"
    include.write_text("ok\n", encoding="utf-8")

    real_resolve = Path.resolve

    def _raise_for_requested(self: Path, *args: Any, **kwargs: Any) -> Path:
        # Only fault for the requested file — the controller resolves
        # the config_dir too, which has to keep working for the
        # main_path comparison.
        if self.name == "common.yaml":
            raise OSError("simulated resolve failure")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _raise_for_requested)

    result = controller._resolve_file(str(include), "kitchen.yaml", "")

    assert result == "ok\n"


# ---------------------------------------------------------------------------
# _validate_locked — JSON-decoding tolerance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_locked_skips_unparseable_lines(tmp_path: Path) -> None:
    """A malformed JSON line is dropped; the loop reads the next line.

    The validator may emit a stray non-JSON line (debug log, version
    banner that escaped the startup drain). The loop's
    ``except JSONDecodeError`` keeps reading instead of raising —
    pin the branch so a refactor that moved the loads outside the
    try/except surfaces here.
    """
    controller = _make_controller(tmp_path)
    session = _EditorSession()
    proc, _reader, _ = _make_fake_proc(
        [
            b"this is not json\n",
            dumps({"type": "result", "yaml_errors": [], "validation_errors": []}) + b"\n",
        ]
    )
    session.proc = proc
    controller._ensure_subprocess = AsyncMock()  # type: ignore[method-assign]

    result = await controller._validate_locked(session, "kitchen.yaml", "")

    assert result == {"yaml_errors": [], "validation_errors": []}


# ---------------------------------------------------------------------------
# validate_yaml — public api_command entrypoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_yaml_creates_session_and_delegates(
    tmp_path: Path,
) -> None:
    """First call for a config creates a session and forwards to ``_validate_locked``.

    Each configuration gets exactly one ``_EditorSession`` (so
    concurrent edits on the same YAML are serialised through
    ``session.lock``). Pin both halves: the session lands in
    ``_sessions``, and ``_validate_locked`` receives the same
    instance.
    """
    controller = _make_controller(tmp_path)

    async def _fake_validate(session: _EditorSession, configuration: str, content: str) -> dict:
        # Stash the session on the controller so the test can
        # confirm it's the same instance the registry holds.
        controller._captured_session = session  # type: ignore[attr-defined]
        return {"yaml_errors": [], "validation_errors": []}

    controller._validate_locked = _fake_validate  # type: ignore[method-assign]

    result = await controller.validate_yaml(configuration="kitchen.yaml", content="esphome:\n")

    assert result == {"yaml_errors": [], "validation_errors": []}
    assert "kitchen.yaml" in controller._sessions
    assert controller._captured_session is controller._sessions["kitchen.yaml"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_validate_yaml_terminates_session_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timeout from ``_validate_locked`` → session torn down + re-raise.

    Subprocess wedged or unreachable — kill it so the next call
    spawns fresh. Without the teardown, the next validate call
    would hit the same wedged process and block forever.
    """
    controller = _make_controller(tmp_path)

    async def _hangs(*_args: Any, **_kwargs: Any) -> dict:  # pragma: no cover
        return {}

    controller._validate_locked = _hangs  # type: ignore[method-assign]

    async def _raise_timeout(awaitable: Any, *_args: Any, **_kwargs: Any) -> None:
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(
        "esphome_device_builder.controllers.editor.asyncio.wait_for",
        _raise_timeout,
    )

    terminated = AsyncMock()
    controller._terminate_subprocess = terminated  # type: ignore[method-assign]

    with pytest.raises(TimeoutError):
        await controller.validate_yaml(configuration="kitchen.yaml", content="")

    terminated.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_yaml_terminates_session_on_runtime_error(
    tmp_path: Path,
) -> None:
    """``_validate_locked`` raising ``RuntimeError`` (subprocess died) → teardown + re-raise."""
    controller = _make_controller(tmp_path)

    async def _raise_runtime(*_args: Any, **_kwargs: Any) -> dict:
        raise RuntimeError("subprocess closed stdout")

    controller._validate_locked = _raise_runtime  # type: ignore[method-assign]
    terminated = AsyncMock()
    controller._terminate_subprocess = terminated  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="closed stdout"):
        await controller.validate_yaml(configuration="kitchen.yaml", content="")

    terminated.assert_awaited_once()
