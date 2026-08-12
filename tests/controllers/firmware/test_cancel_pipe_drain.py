"""``_pump_output_until_exit`` exit and drain semantics when a descendant holds the pipe."""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware import helpers as helpers_module
from esphome_device_builder.controllers.firmware.helpers import _pump_output_until_exit
from esphome_device_builder.helpers.subprocess import create_subprocess_exec
from tests.controllers.firmware.conftest import kill_pid

from ...conftest import wait_until

_GRANDCHILD_HOLDING_PIPE_SCRIPT = (
    "import subprocess, sys; "
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
    "close_fds=False); "
    "print(f'GRANDCHILD_PID={child.pid}', flush=True); "
    "print('LINE_BEFORE_EXIT', flush=True)"
)


async def test_cancelled_job_finalizes_despite_pipe_held_open() -> None:
    """Cancel + dead parent + grandchild holding stdout → pump returns after the drain window."""
    lines: list[str] = []
    proc = await create_subprocess_exec(
        sys.executable,
        "-c",
        _GRANDCHILD_HOLDING_PIPE_SCRIPT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        exit_code = await asyncio.wait_for(
            _pump_output_until_exit("cancel-1", proc, lines.append, {"cancel-1"}),
            timeout=15.0,
        )
    finally:
        for line in lines:
            if line.startswith("GRANDCHILD_PID="):
                kill_pid(int(line.split("=", 1)[1]))
        # The grandchild's death EOFs the pipe; drain it and give the
        # disconnect callbacks a few ticks so the transport closes
        # inside this loop rather than warning at GC time.
        assert proc.stdout is not None
        with suppress(TimeoutError):
            await asyncio.wait_for(proc.stdout.read(), timeout=5.0)
        await proc.wait()
        for _ in range(3):
            await asyncio.sleep(0)
    assert exit_code == 0
    # Output written before the parent exited still made it through.
    assert any("LINE_BEFORE_EXIT" in line for line in lines)


async def test_uncancelled_job_reads_to_eof() -> None:
    """No cancel → the pump drains the full output to EOF, exactly like the inline loop."""
    lines: list[str] = []
    proc = await create_subprocess_exec(
        sys.executable,
        "-c",
        "print('one'); print('two'); print('three')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    exit_code = await asyncio.wait_for(
        _pump_output_until_exit("normal-1", proc, lines.append, set()),
        timeout=15.0,
    )
    assert exit_code == 0
    assert [line.strip() for line in lines if line.strip()] == ["one", "two", "three"]


async def test_uncancelled_job_with_held_pipe_warns_and_waits_for_eof(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reaped process + held pipe + no cancel → one warning, EOF still decides."""
    monkeypatch.setattr(helpers_module, "_EXIT_POLL_SECONDS", 0.05)
    lines: list[str] = []
    proc = await create_subprocess_exec(
        sys.executable,
        "-c",
        _GRANDCHILD_HOLDING_PIPE_SCRIPT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    task = asyncio.get_running_loop().create_task(
        _pump_output_until_exit("normal-2", proc, lines.append, set())
    )
    logger_name = "esphome_device_builder.controllers.firmware.helpers"
    try:
        with caplog.at_level("WARNING", logger=logger_name):
            await wait_until(
                lambda: "holds the output pipe" in caplog.text,
                10.0,
                "wedged-pipe warning",
                interval=0.05,
            )
        assert not task.done()
    finally:
        for line in lines:
            if line.startswith("GRANDCHILD_PID="):
                kill_pid(int(line.split("=", 1)[1]))
        exit_code = await asyncio.wait_for(task, timeout=10.0)
    assert exit_code == 0
    assert sum("holds the output pipe" in rec.getMessage() for rec in caplog.records) == 1


async def test_poll_continues_until_process_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pump keeps polling while the process runs; the reap unblocks the cancel exit."""
    monkeypatch.setattr(helpers_module, "_EXIT_POLL_SECONDS", 0)
    monkeypatch.setattr(helpers_module, "_CANCELLED_PIPE_DRAIN_SECONDS", 0)
    never_eof = asyncio.StreamReader()
    proc = MagicMock(stdout=never_eof, returncode=None)
    task = asyncio.get_running_loop().create_task(
        _pump_output_until_exit("cancel-3", proc, lambda _line: None, {"cancel-3"})
    )
    await asyncio.sleep(0.02)
    assert not task.done()

    proc.returncode = 3
    assert await asyncio.wait_for(task, timeout=5.0) == 3


async def test_outer_cancellation_propagates_through_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the pumping task mid-drain isn't swallowed by the reader teardown."""
    # Zero the poll so the pump falls through to the drain wait on its
    # first iteration; stretch the drain so the cancel lands inside it.
    monkeypatch.setattr(helpers_module, "_EXIT_POLL_SECONDS", 0)
    monkeypatch.setattr(helpers_module, "_CANCELLED_PIPE_DRAIN_SECONDS", 30.0)
    never_eof = asyncio.StreamReader()
    proc = MagicMock(stdout=never_eof, returncode=0)
    task = asyncio.get_running_loop().create_task(
        _pump_output_until_exit("cancel-2", proc, lambda _line: None, {"cancel-2"})
    )
    # Let the pump reach the drain wait (proc exits instantly; the
    # reader never EOFs), then cancel the pumping task itself.
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
