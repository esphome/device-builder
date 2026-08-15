"""Suppress Windows hard-error dialogs for this process and every subprocess it spawns.

Child processes inherit the parent's error mode (CreateProcess without
CREATE_DEFAULT_ERROR_MODE, which Python's subprocess never passes), so setting
it once here covers the whole build chain down to the toolchain binaries.
Without it, binutils ``ar.exe`` raises a modal "Bad Image" dialog per
invocation when a toolchain package ships a non-DLL in ``lib/bfd-plugins/``
(binutils bug 27113; ``libdep.a`` in toolchain-rp2040-earlephilhower, #2562).
"""

from __future__ import annotations

import sys

SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002
SEM_NOOPENFILEERRORBOX = 0x8000

_SUPPRESS_DIALOG_FLAGS = SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX


def suppress_child_error_dialogs() -> None:
    """Disable hard-error dialog boxes for this process tree; no-op off Windows."""
    if sys.platform != "win32":
        return
    import ctypes  # noqa: PLC0415

    kernel32 = ctypes.windll.kernel32
    kernel32.SetErrorMode(kernel32.GetErrorMode() | _SUPPRESS_DIALOG_FLAGS)
