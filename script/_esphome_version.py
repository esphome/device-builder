"""Shared ESPHome-version guard for the catalog sync scripts.

``sync_boards.py`` and ``sync_components.py`` both derive platform metadata
from the *installed* ESPHome, so each must run against the version its output
records, or it ships metadata from one ESPHome stamped with another's version.
This centralizes the "installed == expected, else exit with install
instructions" check both perform.
"""

from __future__ import annotations

import sys
from collections.abc import Callable


def assert_installed_esphome(
    expected: str,
    *,
    what: str,
    normalize: Callable[[str], str] | None = None,
    alt_fix: str | None = None,
) -> None:
    """
    Exit unless the installed ESPHome matches *expected*.

    *what* names the caller for the message; *normalize* (e.g. beta -> base) is
    applied to both sides before comparing; *alt_fix* appends an extra
    remediation line to the mismatch message.
    """
    canon = normalize or (lambda v: v)
    install = f"    uv pip install 'esphome=={expected}'   # or: pip install 'esphome=={expected}'"
    try:
        from esphome.const import __version__ as installed
    except ImportError:
        raise SystemExit(
            f"{what}: ESPHome is not importable in this interpreter ({sys.executable}).\n"
            f"To fix, install it into the project venv and re-run:\n{install}"
        ) from None
    if canon(installed) != canon(expected):
        msg = (
            f"{what}: needs ESPHome {expected}, but {installed} is installed.\n"
            f"To fix, install the matching version into this venv and re-run:\n{install}"
        )
        if alt_fix:
            msg += f"\n{alt_fix}"
        raise SystemExit(msg)
