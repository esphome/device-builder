"""
Major-version match helper for the offloader's compat gate.

Mirrors the frontend's ``classifyVersionMismatch`` so the
gate's verdict matches the version-skew warning the operator
sees on the pairing row.
"""

from __future__ import annotations

import re

_DIGITS_PREFIX_RE = re.compile(r"^(\d+)")


def major_versions_match(local: str, peer: str) -> bool:
    """
    Return ``True`` when *local* and *peer* share a ``YYYY.MM`` release line.

    Empty strings on either side match so a fresh APPROVED
    pairing isn't filtered before its first session-open.
    """
    if not local or not peer:
        return True
    if local == peer:
        return True
    return _release_key(local) == _release_key(peer)


def _release_key(version: str) -> str:
    """Year + month prefix used for cross-release comparison."""
    parts = version.split(".")
    year = parts[0] if parts else ""
    month_raw = parts[1] if len(parts) > 1 else ""
    match = _DIGITS_PREFIX_RE.match(month_raw)
    month = match.group(1) if match else month_raw
    return f"{year}.{month}"
