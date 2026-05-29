r"""Cross-OS path utilities for receiver-shipped strings.

The remote-build wire ships absolute paths from the receiver's
filesystem. On a POSIX offloader handling a Windows receiver's
``C:\...`` string, ``Path`` is ``PosixPath`` and treats
backslashes as literal characters, so ``relative_to`` /
``.parent`` / ``.name`` all misfire. Helpers here detect the
receiver's flavour and parse accordingly.
"""

from __future__ import annotations

import re
from pathlib import PurePath, PurePosixPath, PureWindowsPath

# Drive-letter (``C:\``) or UNC (``\\server\``) prefix.
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def receiver_pure_path_cls(receiver_path: str) -> type[PurePath]:
    """Return the ``PurePath`` flavour matching *receiver_path*'s OS shape."""
    if _WINDOWS_ABSOLUTE_PATH_RE.match(receiver_path):
        return PureWindowsPath
    return PurePosixPath


def cross_os_basename(path: str) -> str:
    """Return *path*'s trailing component, splitting on both POSIX and Windows separators."""
    return path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
