"""Remove secrets values from text that leaves the dashboard."""

from __future__ import annotations

import re
from typing import Any

REDACTED_MARKER = "<removed>"
# Shorter values and line fragments (ports, small ids, a PEM tail) would shred unrelated output.
_MIN_REDACTED_SECRET_LEN = 6


def redact_secret_values(lines: list[str], mappings: list[dict]) -> list[str]:
    """Replace each *mappings* scalar of ``_MIN_REDACTED_SECRET_LEN``+ characters in *lines*."""
    # Output arrives one line at a time, so a multi-line value must match by line.
    values = {
        part
        for text in _scalar_leaves(mappings)
        for part in text.splitlines()
        if len(part) >= _MIN_REDACTED_SECRET_LEN and part.strip()
    }
    if not values:
        return lines
    # Longest first: alternation takes the first branch that matches.
    longest_first = sorted(values, key=lambda v: (-len(v), v))
    pattern = re.compile("|".join(map(re.escape, longest_first)))
    return [pattern.sub(REDACTED_MARKER, line) for line in lines]


def _scalar_leaves(value: Any) -> list[str]:
    """Return every scalar inside *value* as text, walking lists and mappings."""
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in _scalar_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in _scalar_leaves(item)]
    if value is None or isinstance(value, bool):
        return []
    return [str(value)]
