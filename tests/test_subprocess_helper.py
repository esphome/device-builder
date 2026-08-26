"""Tests for the centralised subprocess spawn helper."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import esphome_device_builder
from esphome_device_builder.helpers import subprocess as subprocess_helper


async def test_create_subprocess_exec_forces_close_fds_false() -> None:
    """The wrapper must always pass ``close_fds=False`` even when the caller doesn't."""
    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock:
        await subprocess_helper.create_subprocess_exec(
            "echo",
            "hi",
            stdout=asyncio.subprocess.PIPE,
        )

    args, kwargs = mock.call_args
    assert args == ("echo", "hi")
    assert kwargs["close_fds"] is False
    assert kwargs["stdout"] == asyncio.subprocess.PIPE


async def test_create_subprocess_exec_caller_close_fds_is_overridden() -> None:
    """Callers can't accidentally restore the slow default."""
    with patch.object(
        asyncio,
        "create_subprocess_exec",
        new_callable=AsyncMock,
    ) as mock:
        # If a caller passes close_fds=True, the helper still overrides it
        # by explicitly setting kwargs["close_fds"] = False before delegating
        # to asyncio. Documented here so a future refactor preserves the
        # actual mechanism, not the wrong "later kwarg wins" rationale.
        await subprocess_helper.create_subprocess_exec("echo", "hi", close_fds=True)

    _, kwargs = mock.call_args
    assert kwargs["close_fds"] is False


async def test_create_subprocess_exec_actually_runs() -> None:
    """End-to-end smoke: the helper produces a working ``Process``."""
    proc = await subprocess_helper.create_subprocess_exec(
        sys.executable,
        "-c",
        "print('subprocess-helper-ok')",
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    assert proc.returncode == 0
    assert b"subprocess-helper-ok" in stdout


async def test_run_subprocess_capture_passes_stdin_data() -> None:
    """``stdin_data`` is written to the child and echoed back on stdout."""
    result = await subprocess_helper.run_subprocess_capture(
        sys.executable,
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read())",
        timeout=10,
        stdin_data=b"hello-stdin",
    )
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.stdout == b"hello-stdin"


async def test_run_subprocess_capture_discards_stderr_when_not_merged() -> None:
    """``merge_stderr=False`` keeps stderr out of the captured stdout."""
    result = await subprocess_helper.run_subprocess_capture(
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('noise'); sys.stdout.write('out')",
        timeout=10,
        merge_stderr=False,
    )
    assert result.stdout == b"out"


async def test_run_subprocess_capture_merges_stderr_by_default() -> None:
    """The default folds stderr into stdout for a unified stream."""
    result = await subprocess_helper.run_subprocess_capture(
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('noise'); sys.stdout.write('out')",
        timeout=10,
    )
    assert b"noise" in result.stdout
    assert b"out" in result.stdout


async def test_run_subprocess_capture_timeout_returns_partial_output() -> None:
    """A timed-out child's pre-timeout stdout survives as the diagnostic."""
    result = await subprocess_helper.run_subprocess_capture(
        sys.executable,
        "-c",
        "import time; print('EARLY', flush=True); time.sleep(30)",
        timeout=0.5,
    )
    assert result.timed_out is True
    assert b"EARLY" in result.stdout


async def test_run_subprocess_capture_on_line_streams_and_stdout_empty() -> None:
    """``on_line`` receives each chunk; the result's stdout stays empty."""
    lines: list[str] = []
    result = await subprocess_helper.run_subprocess_capture(
        sys.executable,
        "-c",
        "print('a'); print('b')",
        timeout=10,
        on_line=lines.append,
    )
    assert result.returncode == 0
    assert result.stdout == b""
    assert [line.rstrip("\r\n") for line in lines] == ["a", "b"]


async def test_run_subprocess_capture_on_line_exception_kills_child() -> None:
    """An exception escaping *on_line* propagates and the child is killed."""

    def _boom(line: str) -> None:
        raise RuntimeError("listener boom")

    with pytest.raises(RuntimeError, match="listener boom"):
        await subprocess_helper.run_subprocess_capture(
            sys.executable,
            "-c",
            "import time; print('hello', flush=True); time.sleep(30)",
            timeout=30,
            on_line=_boom,
        )
    # Let the child watcher reap the SIGKILL'd process before the
    # test loop closes, or its transport __del__ warns at teardown.
    await asyncio.sleep(0.1)


async def test_run_subprocess_capture_timeout_with_stdin_pending() -> None:
    """Timeout with a stdin writer in flight still returns cleanly."""
    result = await subprocess_helper.run_subprocess_capture(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        timeout=0.5,
        stdin_data=b"unread",
    )
    assert result.timed_out is True


async def test_run_subprocess_capture_cancel_kills_child() -> None:
    """Cancelling the awaiting task kills the child and propagates the cancel."""
    task = asyncio.get_running_loop().create_task(
        subprocess_helper.run_subprocess_capture(
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            timeout=60,
            stdin_data=b"unread",
        )
    )
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    # Let the child watcher reap the SIGKILL'd process before the
    # test loop closes, or its transport __del__ warns at teardown.
    await asyncio.sleep(0.1)


async def test_run_subprocess_capture_tolerates_child_ignoring_stdin() -> None:
    """A child exiting without draining a large stdin doesn't raise."""
    result = await subprocess_helper.run_subprocess_capture(
        sys.executable,
        "-c",
        "pass",
        timeout=10,
        stdin_data=b"x" * (8 * 1024 * 1024),
    )
    assert result.timed_out is False
    assert result.returncode == 0


def test_no_call_site_uses_asyncio_create_subprocess_exec_directly() -> None:
    """Guard against regressions: no callsite should bypass the helper.

    Catches future commits that re-introduce a direct
    ``asyncio.create_subprocess_exec`` call (which would skip the
    ``close_fds=False`` optimisation) anywhere outside the helper itself.
    """
    pkg_root = Path(esphome_device_builder.__file__).parent
    helper_path = pkg_root / "helpers" / "subprocess.py"
    needle = b"asyncio.create_subprocess_exec"

    # Bytes-mode short-circuit: most files don't contain the needle, so
    # one ``in`` check on the file blob beats decoding + walking every
    # line. Run as a sync test so blockbuster doesn't wrap each
    # ``read_bytes`` on Linux CI.
    offenders: list[str] = []
    for path in pkg_root.rglob("*.py"):
        if path == helper_path:
            continue
        blob = path.read_bytes()
        if needle not in blob:
            continue
        for lineno, line in enumerate(blob.splitlines(), start=1):
            if needle in line:
                offenders.append(f"{path.relative_to(pkg_root)}:{lineno}: {line.decode().strip()}")

    assert not offenders, (
        "Found direct asyncio.create_subprocess_exec calls — use "
        "esphome_device_builder.helpers.subprocess.create_subprocess_exec "
        "instead so close_fds=False is applied:\n  " + "\n  ".join(offenders)
    )


async def test_live_child_pids_tracks_until_reaped() -> None:
    """A spawn is listed while live and drops out once the loop reaps it."""
    proc = await subprocess_helper.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdin.read()",
        stdin=asyncio.subprocess.PIPE,
    )
    assert proc.pid in subprocess_helper.live_child_pids()
    assert proc.stdin is not None
    proc.stdin.close()
    await proc.wait()
    assert proc.pid not in subprocess_helper.live_child_pids()
