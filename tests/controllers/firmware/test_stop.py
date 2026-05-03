"""Stop-button cancellation kills the *whole* esphome compile tree.

Without this, ``proc.terminate()`` / ``proc.kill()`` only signals the
python esphome parent. PlatformIO + gcc grandchildren get orphaned and
the build keeps running until they finish on their own — exactly the
"hit Stop, build kept going" symptom from production.

Drives the regression by spawning a tiny shell script that itself
spawns a long-running grandchild, calling our ``_terminate_current_process``,
and asserting both pids are gone shortly after.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from functools import partial
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.firmware import FirmwareController
from esphome_device_builder.controllers.firmware import controller as _firmware_controller_mod
from esphome_device_builder.helpers import process as _process_mod
from esphome_device_builder.helpers.process import _signal_process_group
from esphome_device_builder.helpers.subprocess import create_subprocess_exec

# Process groups, ``os.fork``, and ``SIGKILL`` are POSIX-only. The
# whole stop-button cancellation strategy here (``killpg`` against the
# subtree's session leader) doesn't apply on Windows, where job
# objects are the equivalent primitive — skip the module wholesale.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process-group / fork / SIGKILL semantics — not applicable on Windows.",
)


def _is_alive(pid: int) -> bool:
    """Return True if *pid* is still running. Survives EPERM on macOS."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it — for our test that
        # only happens if reused as a system pid (negligible odds).
        return True
    return True


async def _wait_dead(pid: int, timeout: float = 3.0) -> bool:
    """Poll until *pid* exits or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return _is_alive(pid) is False


# ---------------------------------------------------------------------------
# _signal_process_group helper
# ---------------------------------------------------------------------------


def test_signal_process_group_returns_false_for_dead_pid() -> None:
    """A pid that already exited is treated as 'nothing to signal' — no exception."""
    # Spawn + wait + reap, then try to signal. The pid is now dead so
    # ``os.getpgid`` raises ``ProcessLookupError`` and our helper has
    # to return False rather than propagate.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    proc.wait()
    assert _signal_process_group(proc.pid, signal.SIGTERM) is False


def test_signal_process_group_returns_false_when_killpg_says_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.killpg`` racing with the child exiting also returns False.

    There's a TOCTOU window between ``os.getpgid`` succeeding and
    ``os.killpg`` firing — the process can exit in between, in
    which case ``killpg`` raises ``ProcessLookupError``. The
    earlier dead-pid test catches the ``getpgid`` arm; this one
    pins the ``killpg`` arm so a refactor that drops it (e.g.
    "the getpgid check already protects us") would surface here.
    """
    monkeypatch.setattr(
        "esphome_device_builder.helpers.process.os.getpgid",
        lambda _pid: 12345,
    )

    def _raise_lookup(_pgid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(
        "esphome_device_builder.helpers.process.os.killpg",
        _raise_lookup,
    )

    assert _signal_process_group(99999, signal.SIGTERM) is False


def test_signal_process_group_returns_false_on_permission_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``EPERM`` from ``killpg`` is logged and returns False, not raised.

    Happens when something downgrades our privileges between
    spawn and signal (rare in production, but the branch exists
    for defense in depth). The user-visible contract is "Stop
    didn't kill the build" rather than "the controller crashed";
    pin both halves — return value and the warning log.
    """
    monkeypatch.setattr(
        "esphome_device_builder.helpers.process.os.getpgid",
        lambda _pid: 12345,
    )

    def _raise_perm(_pgid: int, _sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(
        "esphome_device_builder.helpers.process.os.killpg",
        _raise_perm,
    )

    with caplog.at_level("WARNING", logger="esphome_device_builder.helpers.process"):
        assert _signal_process_group(99999, signal.SIGTERM) is False

    assert any("Permission denied signalling pgid" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# _terminate_current_process — full integration
# ---------------------------------------------------------------------------


@pytest.fixture
def controller() -> FirmwareController:
    """Stand up a FirmwareController shell — only the bits termination touches."""
    ctrl = FirmwareController.__new__(FirmwareController)
    ctrl._current_process = None  # type: ignore[attr-defined]
    ctrl._current_job = MagicMock(job_id="test-job")  # type: ignore[attr-defined]
    return ctrl


async def test_terminate_kills_grandchild_via_process_group(
    controller: FirmwareController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel-while-compiling must kill platformio/gcc grandchildren too.

    Mirrors the real failure: ``esphome run`` forks ``platformio`` which
    forks ``gcc``. ``proc.terminate()`` only hits the direct child, so
    the toolchain runs on regardless. Group-signalling fixes that.
    """
    # Production grace is 3s so well-behaved tools can flush on SIGTERM;
    # this test deliberately traps SIGTERM, so the grace is pure dead
    # time. Wrap the controller's binding so the SIGKILL escalation
    # fires after 0.1s instead of the full window.
    monkeypatch.setattr(
        _firmware_controller_mod,
        "terminate_subtree_with_grace",
        partial(_process_mod.terminate_subtree_with_grace, grace_seconds=0.1),
    )
    # Parent spawns a grandchild that traps SIGTERM (so a parent-only
    # signal would not cascade) and prints its pid for the assertion.
    script = (
        "import os, signal, sys, time\n"
        "p = os.fork()\n"
        "if p == 0:\n"
        # Grandchild: trap SIGTERM as no-op, then sleep forever.
        # Without process-group signalling our terminate path can't
        # reach it.
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    print(f'GRANDCHILD={os.getpid()}', flush=True)\n"
        "    time.sleep(60)\n"
        "    sys.exit(0)\n"
        "else:\n"
        # Parent: print its own pid then wait. SIGTERM-traps too so it
        # only exits when SIGKILL escalates — the controller's grace
        # window is short enough that the test still completes fast.
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    print(f'PARENT={os.getpid()}', flush=True)\n"
        "    os.waitpid(p, 0)\n"
    )
    proc = await create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    controller._current_process = proc  # type: ignore[attr-defined]

    try:
        # Read the two pid lines from stdout so we know what to verify.
        parent_pid: int | None = None
        grandchild_pid: int | None = None
        deadline = time.monotonic() + 5.0
        assert proc.stdout is not None
        while time.monotonic() < deadline and (parent_pid is None or grandchild_pid is None):
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
            if not line:
                break
            text = line.decode().strip()
            if text.startswith("PARENT="):
                parent_pid = int(text.split("=", 1)[1])
            elif text.startswith("GRANDCHILD="):
                grandchild_pid = int(text.split("=", 1)[1])
        assert parent_pid is not None, "child never reported its pid"
        assert grandchild_pid is not None, "grandchild never reported its pid"
        assert _is_alive(parent_pid)
        assert _is_alive(grandchild_pid)

        # Hit Stop. Both pids must die — SIGTERM is ignored, so the
        # controller's grace window expires and SIGKILL escalates to
        # the whole process group.
        await controller._terminate_current_process()
        await proc.wait()

        assert await _wait_dead(parent_pid), f"parent pid {parent_pid} still alive after stop"
        grandchild_alive = not await _wait_dead(grandchild_pid)
        assert not grandchild_alive, (
            f"grandchild pid {grandchild_pid} still alive after stop — "
            "process-group signal didn't reach it"
        )
    finally:
        # Belt-and-suspenders cleanup: if any assertion above failed
        # before the SIGKILL chain ran, the SIGTERM-trapped grandchild
        # would otherwise sleep for 60s and pollute the suite. SIGKILL
        # the whole group regardless of test outcome;
        # ``_signal_process_group`` no-ops for already-dead pids.
        _signal_process_group(proc.pid, signal.SIGKILL)
        with suppress(asyncio.TimeoutError, ProcessLookupError):
            await asyncio.wait_for(proc.wait(), timeout=5.0)
