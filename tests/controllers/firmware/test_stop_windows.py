"""Stop-button cancellation on Windows: job object first, ``taskkill /F /T`` fallback.

The POSIX path (``test_firmware_stop.py``) relies on process groups
and ``killpg`` — primitives that don't exist on Windows. This module
covers the Windows-specific branch in ``_terminate_job_process``:
``TerminateJobObject`` kills the whole tree atomically; ``taskkill``
walks the kernel's parent-PID tree when no job object was assigned.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.controllers.firmware._state import SpawnHandle
from esphome_device_builder.helpers import process as process_module
from esphome_device_builder.helpers.process import _terminate_subtree_windows
from esphome_device_builder.helpers.subprocess import create_subprocess_exec
from esphome_device_builder.helpers.windows_job_object import WindowsJobObject
from tests.controllers.firmware.conftest import (
    BareFirmwareControllerFactory,
    pid_alive,
    wait_dead,
)

# Only the integration tests below — which spawn real subprocesses
# and exercise ``_terminate_job_process``'s Windows branch end
# to end — need the Windows-only guard. The unit tests for
# ``_terminate_subtree_windows`` patch out ``create_subprocess_exec``
# entirely, so they're cross-platform-safe and contribute Windows-
# branch coverage on every OS in the matrix.
windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only termination path; POSIX is covered in test_firmware_stop.py.",
)


@pytest.fixture
def controller(
    bare_firmware_controller_factory: BareFirmwareControllerFactory,
) -> FirmwareController:
    """Stand up a FirmwareController shell — only the bits termination touches."""
    return bare_firmware_controller_factory()


@windows_only
async def test_terminate_kills_grandchild_via_job_object(
    controller: FirmwareController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job object alone takes down a grandchild when the taskkill sweep fails.

    The spawned parent blocks on stdin until the job assignment has
    landed, so the grandchild is deterministically a job member; the
    sweep is forced to fail so only ``TerminateJobObject`` can kill.
    """

    async def _sweep_fails(_pid: int) -> bool:
        return False

    monkeypatch.setattr(process_module, "_terminate_subtree_windows", _sweep_fails)
    proc = await create_subprocess_exec(
        sys.executable,
        "-c",
        "import subprocess, sys; "
        "sys.stdin.readline(); "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "print(f'GRANDCHILD_PID={child.pid}', flush=True); "
        "child.wait()",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    win_job = WindowsJobObject.create_for_pid(proc.pid)
    assert win_job is not None
    job = MagicMock(job_id="test-job")
    controller.state.spawns[job.job_id] = SpawnHandle(proc, win_job)

    try:
        assert proc.stdin is not None
        proc.stdin.write(b"\n")
        await proc.stdin.drain()
        assert proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        grandchild_pid = int(line.decode().split("=", 1)[1])
        assert pid_alive(grandchild_pid)

        await controller._terminate_job_process(job)

        await asyncio.wait_for(proc.wait(), timeout=5.0)
        await wait_dead(grandchild_pid)
    finally:
        win_job.terminate()
        win_job.close()
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@windows_only
async def test_terminate_kills_subprocess_via_taskkill(
    controller: FirmwareController,
) -> None:
    """The Windows stop path force-kills the running subprocess via taskkill /F /T."""
    proc = await create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    job = MagicMock(job_id="test-job")
    controller.state.spawns[job.job_id] = SpawnHandle(proc)

    try:
        await controller._terminate_job_process(job)
        # taskkill /F /T schedules termination synchronously; the
        # subprocess should exit within seconds.
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        assert proc.returncode is not None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def test_terminate_subtree_windows_returns_true_on_taskkill_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean ``taskkill`` exit (returncode 0) reports success.

    Pin the happy-exit branch so the orchestrator doesn't fall
    through to the ``proc.kill()`` fallback on a successful
    ``taskkill /F /T``.
    """

    class _FakeProc:
        returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    fake = _FakeProc()

    async def _spawn(*_args: object, **_kwargs: object) -> _FakeProc:
        return fake

    monkeypatch.setattr(process_module, "create_subprocess_exec", _spawn)
    assert await _terminate_subtree_windows(12345) is True


async def test_terminate_subtree_windows_returns_false_when_taskkill_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``taskkill`` is logged and reported, not raised."""

    async def _missing(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    # Patch the symbol as imported in the firmware module so the
    # production code path (which goes through the helpers wrapper)
    # actually exercises the fallback branch.
    monkeypatch.setattr(process_module, "create_subprocess_exec", _missing)
    assert await _terminate_subtree_windows(12345) is False


async def test_terminate_subtree_windows_returns_false_on_taskkill_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero ``taskkill`` exit (access denied, missing pid, ...) reports failure."""

    class _FakeProc:
        returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 128
            return 128

        def kill(self) -> None:  # pragma: no cover — only used on timeout
            pass

    fake = _FakeProc()

    async def _spawn(*_args: object, **_kwargs: object) -> _FakeProc:
        return fake

    monkeypatch.setattr(process_module, "create_subprocess_exec", _spawn)
    assert await _terminate_subtree_windows(12345) is False


async def test_terminate_subtree_windows_returns_false_on_taskkill_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``taskkill`` itself hanging past the grace window → kill it, report failure.

    Pathological case: ``taskkill`` is on disk and spawns, but
    never returns (driver hung holding the pid open, etc.). The
    helper has to put ``taskkill`` itself down via ``kill_quietly``
    so it doesn't strand a zombie, then return False so the caller
    falls back to ``proc.kill()`` on the original process. Pin
    both halves: kill_quietly fires on the spawned ``taskkill``,
    return value is False.
    """

    class _HungProc:
        returncode: int | None = None
        kill_calls = 0

        async def wait(self) -> int:  # pragma: no cover — wait_for short-circuits
            return 0

        def kill(self) -> None:
            self.kill_calls += 1

    hung = _HungProc()

    async def _spawn(*_args: object, **_kwargs: object) -> _HungProc:
        return hung

    monkeypatch.setattr(process_module, "create_subprocess_exec", _spawn)

    async def _raise_timeout(awaitable: object, *_args: object, **_kwargs: object) -> None:
        # Close the awaitable so a "coroutine was never awaited"
        # warning doesn't fire on the never-consumed wait().
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(process_module.asyncio, "wait_for", _raise_timeout)

    assert await _terminate_subtree_windows(12345) is False
    # ``kill_quietly`` was called on the spawned ``taskkill`` — the
    # helper imports it as a top-level reference, so the call
    # surfaces here as ``hung.kill()``.
    assert hung.kill_calls == 1
