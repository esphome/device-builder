"""A stop signal exits the dashboard cleanly, not via the OS default.

``main`` traps SIGTERM (the POSIX startup window, before aiohttp arms its
run-loop handler) and, on Windows, SIGBREAK (the desktop quits the backend
with CTRL_BREAK_EVENT; aiohttp installs no handler there at all). A signal
that lands while serving drains ``on_cleanup`` (``stop()``) before exiting.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from aiohttp.web import GracefulExit

from esphome_device_builder import __main__ as main_module

if TYPE_CHECKING:
    from pathlib import Path

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM disposition")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows CTRL_BREAK path")

# Logged at the top of ``DeviceBuilder.stop()``; present in a process's
# output only if the graceful ``on_cleanup`` drain ran, so its absence
# distinguishes a clean shutdown from an abrupt OS default-terminate.
_DRAIN_MARKER = "Shutting down ESPHome Device Builder"


def test_exit_cleanly_on_signal_without_loop_raises_zero() -> None:
    """With no running loop the handler raises ``SystemExit(0)``."""
    with pytest.raises(SystemExit) as excinfo:
        main_module._exit_cleanly_on_signal(signal.SIGTERM, None)
    assert excinfo.value.code == 0


async def test_exit_cleanly_on_signal_with_loop_defers_graceful_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a running loop the handler defers ``GracefulExit`` instead of raising inline."""
    scheduled: list[object] = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "call_soon_threadsafe", lambda cb, *a: scheduled.append(cb))
    main_module._exit_cleanly_on_signal(signal.SIGTERM, None)
    assert scheduled == [main_module._raise_graceful_exit]


def test_raise_graceful_exit_raises_graceful_exit() -> None:
    """The deferred callback raises aiohttp's ``GracefulExit``."""
    with pytest.raises(GracefulExit):
        main_module._raise_graceful_exit()


def test_exit_cleanly_on_signal_sets_stop_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """The trap latches ``_stop_requested`` so ``main`` can recognise the stop."""
    monkeypatch.setattr(main_module, "_stop_requested", False)
    with pytest.raises(SystemExit):
        main_module._exit_cleanly_on_signal(signal.SIGTERM, None)
    assert main_module._stop_requested is True


def test_serve_until_stop_returns_on_clean_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normally-returning ``run`` propagates nothing."""
    monkeypatch.setattr(main_module, "_stop_requested", False)
    builder = SimpleNamespace(run=lambda: None)
    main_module._serve_until_stop(builder)  # type: ignore[arg-type]


def test_serve_until_stop_skips_run_when_stop_already_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop swallowed during startup is honoured before serving (``run`` is skipped)."""
    monkeypatch.setattr(main_module, "_stop_requested", True)
    ran: list[bool] = []
    main_module._serve_until_stop(SimpleNamespace(run=lambda: ran.append(True)))  # type: ignore[arg-type]
    assert ran == []


def test_serve_until_stop_swallows_error_when_stop_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A teardown error during a pending stop is a clean exit, not a crash."""
    monkeypatch.setattr(main_module, "_stop_requested", True)

    def _boom() -> None:
        raise RuntimeError("half-started teardown failure")

    main_module._serve_until_stop(SimpleNamespace(run=_boom))  # type: ignore[arg-type]


def test_serve_until_stop_reraises_without_pending_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine startup crash with no stop pending still propagates."""
    monkeypatch.setattr(main_module, "_stop_requested", False)

    def _boom() -> None:
        raise RuntimeError("real startup failure")

    with pytest.raises(RuntimeError, match="real startup failure"):
        main_module._serve_until_stop(SimpleNamespace(run=_boom))  # type: ignore[arg-type]


def test_main_installs_stop_signal_handlers_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop-signal traps are installed before argparse runs."""
    original_term = signal.getsignal(signal.SIGTERM)
    original_break = signal.getsignal(signal.SIGBREAK) if sys.platform == "win32" else None
    try:
        monkeypatch.setattr(sys, "argv", ["esphome-device-builder", "--version"])
        with pytest.raises(SystemExit):
            main_module.main()
        assert signal.getsignal(signal.SIGTERM) is main_module._exit_cleanly_on_signal
        if sys.platform == "win32":
            assert signal.getsignal(signal.SIGBREAK) is main_module._exit_cleanly_on_signal
    finally:
        signal.signal(signal.SIGTERM, original_term)
        if original_break is not None:
            signal.signal(signal.SIGBREAK, original_break)


def _spawn_dashboard(
    config_dir: Path, *, creationflags: int = 0, env: dict[str, str] | None = None
) -> tuple[subprocess.Popen[str], int]:
    """Launch the dashboard on an ephemeral port; return ``(proc, port)``."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen(  # noqa: S603 — args are fully test-controlled
        [
            sys.executable,
            "-m",
            "esphome_device_builder",
            str(config_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
        env=env,
    )
    return proc, port


# Holds the loop running mid-``AppRunner.setup`` with our SIGTERM trap active —
# the pre-serving startup window. We run ``run_app(handle_signals=False)`` so the
# trap is our handler the whole way; the blocking sleep inside ``setup`` makes a
# signal land here deterministically so CI exercises the window every run.
_CRACK_SITECUSTOMIZE = """
import time as _t
from aiohttp import web_runner as _wr
_orig = _wr.AppRunner.setup
async def _slow(self):
    print("CRACK_OPEN", flush=True)
    _t.sleep(2.0)
    return await _orig(self)
_wr.AppRunner.setup = _slow
"""


def _crack_env(tmp_path: Path) -> dict[str, str]:
    """Build an env whose sitecustomize widens the startup crack."""
    shim = tmp_path / "crack_shim"
    shim.mkdir(exist_ok=True)
    (shim / "sitecustomize.py").write_text(_CRACK_SITECUSTOMIZE)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(shim), env.get("PYTHONPATH", "")])
    return env


# Holds the process inside ``on_startup`` (catalogs loaded, devices not yet)
# and makes the half-started controller's teardown raise a non-CancelledError
# when the stop-driven cancellation lands — the shape that escaped ``run_app``
# and exited 1 instead of 0. Deterministic stand-in for whichever real
# controller's cancel-time teardown raised on the CI runner.
_TEARDOWN_FAIL_SITECUSTOMIZE = """
import asyncio
import esphome_device_builder.controllers.devices.controller as _c
async def _start(self):
    print("STARTUP_HELD", flush=True)
    try:
        # 5s only bounds the worst-case lost-wakeup fallback under the test's
        # 30s communicate timeout; a prompt SIGTERM cancels this far sooner.
        await asyncio.sleep(5)
    except asyncio.CancelledError:
        raise RuntimeError("simulated half-started teardown failure") from None
_c.DevicesController.start = _start
"""


def _teardown_fail_env(tmp_path: Path) -> dict[str, str]:
    """Build an env whose sitecustomize fails a controller's cancel teardown."""
    shim = tmp_path / "teardown_fail_shim"
    shim.mkdir(exist_ok=True)
    (shim / "sitecustomize.py").write_text(_TEARDOWN_FAIL_SITECUSTOMIZE)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(shim), env.get("PYTHONPATH", "")])
    return env


def _wait_for_startup_window(proc: subprocess.Popen[str]) -> list[str]:
    """Block until the first log line, marking the pre-serving startup window.

    Logging is configured *below* the stop-signal trap in ``main``, so any
    output proves the trap is armed; the server binds ~1s later, so a signal
    sent now deterministically lands pre-serving on slow and fast runners.
    Returns the lines consumed so the caller can fold them into a full
    transcript for failure diagnostics.
    """
    assert proc.stdout is not None
    consumed: list[str] = []
    deadline = time.monotonic() + 15
    while True:
        line = proc.stdout.readline()
        consumed.append(line)
        if line.strip():
            return consumed
        assert proc.poll() is None, "process exited during startup"
        assert time.monotonic() < deadline, "no startup output before deadline"


def _wait_for_crack(proc: subprocess.Popen[str]) -> list[str]:
    """Block until the child prints CRACK_OPEN; return the lines consumed."""
    assert proc.stdout is not None
    consumed: list[str] = []
    deadline = time.monotonic() + 20
    while True:
        line = proc.stdout.readline()
        consumed.append(line)
        if "CRACK_OPEN" in line:
            return consumed
        assert line or proc.poll() is None, "process exited before the crack"
        assert time.monotonic() < deadline, "no CRACK_OPEN before deadline"


def _wait_until_serving(proc: subprocess.Popen[str], port: int) -> None:
    """Block until the dashboard accepts a TCP connection on *port*."""
    deadline = time.monotonic() + 20
    while True:
        assert proc.poll() is None, "process exited before it began serving"
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        assert time.monotonic() < deadline, "server did not start before deadline"
        time.sleep(0.1)


@posix_only
@pytest.mark.timeout(60)
def test_sigterm_during_startup_exits_zero(tmp_path: Path) -> None:
    """A real SIGTERM landing mid-startup (pre-serving) exits 0, not 143."""
    proc, _ = _spawn_dashboard(tmp_path)
    captured = _wait_for_startup_window(proc)
    try:
        proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=30)
            captured.append(out)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            captured.append(out)
            pytest.fail(
                "dashboard did not exit within 30s of SIGTERM\n"
                f"--- child transcript ---\n{''.join(captured)}"
            )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    # A negative code is death by signal: -SIGTERM (== shell 143) is the
    # regression this test guards.
    assert proc.returncode == 0, (
        f"expected clean exit 0, got {proc.returncode}\n"
        f"--- child transcript ---\n{''.join(captured)}"
    )


@posix_only
@pytest.mark.timeout(120)
def test_sigterm_in_startup_crack_exits_zero(tmp_path: Path) -> None:
    """A SIGTERM landing while the loop is mid-startup exits 0, not hang or 143."""
    # The window (loop running, our trap active, pre-serving) is timing-
    # dependent in the wild; the shim's blocking sleep inside setup widens it so
    # CI hits it deterministically. The transcript surfaces the child traceback
    # on failure. Looped a few times to also shake out the signal-context
    # reentrancy that only bites when the stop lands mid-log-write.
    env = _crack_env(tmp_path)
    for i in range(3):
        config_dir = tmp_path / f"crack{i}"
        config_dir.mkdir()
        proc, _ = _spawn_dashboard(config_dir, env=env)
        captured = _wait_for_crack(proc)
        time.sleep(0.3)  # land the signal mid the shim's 2s widening sleep
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                out, _ = proc.communicate(timeout=30)
                captured.append(out)
                returncode: int | None = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
                captured.append(out)
                returncode = None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        transcript = "".join(captured)
        assert returncode is not None, (
            f"iteration {i}: dashboard did not exit within 30s of SIGTERM\n"
            f"--- child transcript ---\n{transcript}"
        )
        assert returncode == 0, (
            f"iteration {i}: expected clean exit 0, got {returncode}\n"
            f"--- child transcript ---\n{transcript}"
        )


@posix_only
@pytest.mark.timeout(60)
def test_sigterm_during_startup_with_failing_teardown_exits_zero(tmp_path: Path) -> None:
    """A stop interrupting on_startup exits 0 even if a half-started teardown raises."""
    # aiohttp runs on_startup outside its cleanup try/finally, so a stop-driven
    # cancellation mid-startup can surface a non-CancelledError that escapes
    # run_app. ``main`` treats that as a clean exit because a stop was pending.
    proc, _ = _spawn_dashboard(tmp_path, env=_teardown_fail_env(tmp_path))
    assert proc.stdout is not None
    captured: list[str] = []
    deadline = time.monotonic() + 20
    while True:
        line = proc.stdout.readline()
        captured.append(line)
        if "STARTUP_HELD" in line:
            break
        assert line or proc.poll() is None, "process exited before startup hold"
        assert time.monotonic() < deadline, "no STARTUP_HELD before deadline"
    try:
        proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=30)
            captured.append(out)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            captured.append(out)
            pytest.fail(
                "dashboard did not exit within 30s of SIGTERM\n"
                f"--- child transcript ---\n{''.join(captured)}"
            )
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, (
        f"expected clean exit 0, got {proc.returncode}\n"
        f"--- child transcript ---\n{''.join(captured)}"
    )


@posix_only
@pytest.mark.timeout(60)
def test_sigterm_while_serving_drains_cleanly(tmp_path: Path) -> None:
    """A SIGTERM while serving runs the on_cleanup drain and exits 0."""
    proc, port = _spawn_dashboard(tmp_path)
    out = ""
    try:
        _wait_until_serving(proc, port)
        proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail("dashboard did not exit within 30s of SIGTERM")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, f"expected clean exit 0, got {proc.returncode}"
    assert _DRAIN_MARKER in out, "graceful drain (stop()) did not run"


@windows_only
@pytest.mark.timeout(60)
def test_ctrl_break_while_serving_drains_cleanly(tmp_path: Path) -> None:
    """CTRL_BREAK_EVENT while serving runs the on_cleanup drain and exits 0."""
    proc, port = _spawn_dashboard(tmp_path, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    out = ""
    try:
        _wait_until_serving(proc, port)
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail("dashboard did not exit within 30s of CTRL_BREAK")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, f"expected clean exit 0, got {proc.returncode}"
    assert _DRAIN_MARKER in out, "graceful drain (stop()) did not run"
