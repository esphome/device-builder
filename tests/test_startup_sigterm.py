"""A stop signal exits the dashboard cleanly, not via the OS default.

``main`` traps SIGTERM (the POSIX startup window, before aiohttp arms its
run-loop handler) and, on Windows, SIGBREAK (the desktop quits the backend
with CTRL_BREAK_EVENT; aiohttp installs no handler there at all). A signal
that lands while serving drains ``on_cleanup`` (``stop()``) before exiting.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from esphome_device_builder import __main__ as main_module

if TYPE_CHECKING:
    from pathlib import Path

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM disposition")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows CTRL_BREAK path")

# Logged at the top of ``DeviceBuilder.stop()``; present in a process's
# output only if the graceful ``on_cleanup`` drain ran, so its absence
# distinguishes a clean shutdown from an abrupt OS default-terminate.
_DRAIN_MARKER = "Shutting down ESPHome Device Builder"


def test_exit_cleanly_on_signal_raises_zero() -> None:
    """The handler raises ``SystemExit(0)`` so the interpreter exits cleanly."""
    with pytest.raises(SystemExit) as excinfo:
        main_module._exit_cleanly_on_signal(signal.SIGTERM, None)
    assert excinfo.value.code == 0


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
    config_dir: Path, *, creationflags: int = 0
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
    )
    return proc, port


def _wait_for_startup_window(proc: subprocess.Popen[str]) -> None:
    """Block until the first log line, marking the pre-serving startup window.

    Logging is configured *below* the stop-signal trap in ``main``, so any
    output proves the trap is armed; the server binds ~1s later, so a signal
    sent now deterministically lands pre-serving on slow and fast runners.
    """
    assert proc.stdout is not None
    deadline = time.monotonic() + 15
    while not proc.stdout.readline().strip():
        assert proc.poll() is None, "process exited during startup"
        assert time.monotonic() < deadline, "no startup output before deadline"


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
    try:
        _wait_for_startup_window(proc)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail("dashboard did not exit within 30s of SIGTERM")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    # A negative code is death by signal: -SIGTERM (== shell 143) is the
    # regression this test guards.
    assert proc.returncode == 0, f"expected clean exit 0, got {proc.returncode}"


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
