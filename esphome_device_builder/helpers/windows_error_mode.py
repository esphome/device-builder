"""Suppress Windows hard-error dialogs for this process and every subprocess it spawns.

Child processes inherit the parent's error mode, so setting it once at startup
covers the whole build chain down to the toolchain binaries.
"""

from __future__ import annotations

import sys

_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOGPFAULTERRORBOX = 0x0002
_SEM_NOOPENFILEERRORBOX = 0x8000

SUPPRESS_DIALOG_FLAGS = _SEM_FAILCRITICALERRORS | _SEM_NOGPFAULTERRORBOX | _SEM_NOOPENFILEERRORBOX


def suppress_child_error_dialogs() -> None:
    """Disable hard-error dialog boxes for this process tree; no-op off Windows."""
    if sys.platform != "win32":
        return
    import ctypes  # noqa: PLC0415

    kernel32 = ctypes.windll.kernel32
    kernel32.SetErrorMode(kernel32.GetErrorMode() | SUPPRESS_DIALOG_FLAGS)
