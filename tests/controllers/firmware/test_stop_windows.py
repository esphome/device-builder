"""Stop-button cancellation on Windows uses ``taskkill /F /T``.

The POSIX path (``test_firmware_stop.py``) relies on process groups
and ``killpg`` — primitives that don't exist on Windows. This module
covers the Windows-specific branch in ``_terminate_current_process``:
``taskkill`` walks the kernel's parent-PID tree and force-kills the
whole subtree in one shot.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.helpers import process as process_module
from esphome_device_builder.helpers.process import _terminate_subtree_windows
from esphome_device_builder.helpers.subprocess import create_subprocess_exec

# Only the integration test below — which spawns a real subprocess
# and exercises ``_terminate_current_process``'s Windows branch end
# to end — needs the Windows-only guard. The unit tests for
# ``_terminate_subtree_windows`` patch out ``create_subprocess_exec``
# entirely, so they're cross-platform-safe and contribute Windows-
# branch coverage on every OS in the matrix.
windows_only = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only termination path; POSIX is covered in test_firmware_stop.py.",
)


@pytest.fixture
def controller() -> FirmwareController:
    """Stand up a FirmwareController shell — only the bits termination touches."""
    ctrl = FirmwareController.__new__(FirmwareController)
    ctrl._current_process = None  # type: ignore[attr-defined]
    ctrl._current_job = MagicMock(job_id="test-job")  # type: ignore[attr-defined]
    return ctrl


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
    controller._current_process = proc  # type: ignore[attr-defined]

    try:
        await controller._terminate_current_process()
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
