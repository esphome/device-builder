"""SIGTERM during the startup window exits 0, not 143.

``main`` traps SIGTERM before serving begins, where aiohttp's handler
isn't up yet, so a stop mid cold-start exits cleanly.
"""

from __future__ import annotations

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


def test_exit_on_startup_sigterm_raises_zero() -> None:
    """The handler raises ``SystemExit(0)`` so the interpreter exits cleanly."""
    with pytest.raises(SystemExit) as excinfo:
        main_module._exit_on_startup_sigterm(signal.SIGTERM, None)
    assert excinfo.value.code == 0


def test_main_installs_sigterm_handler_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SIGTERM trap is installed before argparse runs."""
    original = signal.getsignal(signal.SIGTERM)
    try:
        monkeypatch.setattr(sys, "argv", ["esphome-device-builder", "--version"])
        with pytest.raises(SystemExit):
            main_module.main()
        assert signal.getsignal(signal.SIGTERM) is main_module._exit_on_startup_sigterm
    finally:
        signal.signal(signal.SIGTERM, original)


@posix_only
@pytest.mark.timeout(60)
def test_sigterm_during_startup_exits_zero(tmp_path: Path) -> None:
    """A real SIGTERM landing mid-startup (pre-serving) exits 0, not 143."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    proc = subprocess.Popen(  # noqa: S603 — args are fully test-controlled
        [
            sys.executable,
            "-m",
            "esphome_device_builder",
            str(tmp_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    try:
        # Signal on the first log line rather than after a fixed sleep:
        # logging is configured *below* the SIGTERM trap in ``main``, so any
        # output proves the trap is armed, and the server binds ~1s later, so
        # the signal deterministically lands in the startup window on slow and
        # fast runners alike.
        deadline = time.monotonic() + 15
        while not proc.stdout.readline().strip():
            assert proc.poll() is None, "process exited during startup"
            assert time.monotonic() < deadline, "no startup output before deadline"
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
