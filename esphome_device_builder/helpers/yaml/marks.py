"""PyYAML mark (``in "<path>", line N, column M``) helpers for user-facing messages."""

from __future__ import annotations

import re
from typing import NamedTuple

from ..cross_os_path import cross_os_basename

_MARK_RE = re.compile(r'in "(?P<path>[^"\n]+)", line (?P<line>\d+), column (?P<col>\d+)')


class Mark(NamedTuple):
    """One PyYAML mark: the document path verbatim plus its 1-based line and column."""

    path: str
    line: int
    column: int


def parse_marks(message: str) -> list[Mark]:
    """Return every mark in *message*, in order."""
    return [Mark(m["path"], int(m["line"]), int(m["col"])) for m in _MARK_RE.finditer(message)]


def marked_paths(message: str) -> set[str]:
    """Return every document path a mark in *message* points at, verbatim."""
    return {mark.path for mark in parse_marks(message)}


def trim_marks(message: str) -> str:
    """Rewrite marks to ``in <basename>, line N, column M`` and collapse *message* to one line."""
    trimmed = _MARK_RE.sub(
        lambda m: f"in {cross_os_basename(m['path'])}, line {m['line']}, column {m['col']}",
        message,
    )
    return " ".join(trimmed.split())
