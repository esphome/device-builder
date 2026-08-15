"""Tests for ``helpers.windows_error_mode``."""

from __future__ import annotations

import subprocess
import sys

import pytest

from esphome_device_builder.helpers.windows_error_mode import (
    SUPPRESS_DIALOG_FLAGS,
    suppress_child_error_dialogs,
)


@pytest.mark.skipif(sys.platform == "win32", reason="pins the off-Windows no-op contract")
def test_noop_off_windows() -> None:
    """Off Windows the call returns without touching any Win32 API."""
    suppress_child_error_dialogs()


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 error-mode path")
def test_sets_error_mode_and_children_inherit() -> None:
    """The dialog-suppression bits land on this process and propagate to a child."""
    if sys.platform != "win32":  # pragma: no cover (mypy narrows platform via if, not skipif)
        return
    import ctypes  # noqa: PLC0415

    suppress_child_error_dialogs()
    mode = ctypes.windll.kernel32.GetErrorMode()
    assert mode & SUPPRESS_DIALOG_FLAGS == SUPPRESS_DIALOG_FLAGS
    child = subprocess.run(
        [sys.executable, "-c", "import ctypes; print(ctypes.windll.kernel32.GetErrorMode())"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert int(child.stdout.strip()) & SUPPRESS_DIALOG_FLAGS == SUPPRESS_DIALOG_FLAGS
