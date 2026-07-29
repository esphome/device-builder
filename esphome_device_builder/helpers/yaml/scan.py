"""Shared line-scan primitives for the YAML text-splice helpers."""

from __future__ import annotations

import re

# Regex prefix for a key on its list item's dash line (``- <key>: ...``).
DASH_KEY_PREFIX = r"^\s*-\s+"


def key_header_re(key: str, *, indent: str = "") -> re.Pattern[str]:
    """Pattern matching a ``<key>:`` header line at exactly *indent*, bare or with a comment."""
    return key_line_res(key, prefix=f"^{re.escape(indent)}")[0]


def key_line_res(key: str, *, prefix: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """``(block header, inline scalar)`` patterns for ``<key>:`` after regex *prefix*."""
    escaped = re.escape(key)
    return (
        re.compile(rf"{prefix}{escaped}:\s*(?:#.*)?$"),
        re.compile(rf"{prefix}{escaped}:\s*[^\s#]"),
    )


def find_block_header(lines: list[str], key: str) -> int | None:
    """Index of the column-0 ``<key>:`` header line, or ``None`` when absent."""
    header_re = key_header_re(key)
    for idx, line in enumerate(lines):
        if header_re.match(line.rstrip("\n\r")):
            return idx
    return None


def block_end_index(lines: list[str], start: int) -> int:
    """First line after *start* that opens the next top-level block; ``len(lines)`` at EOF."""
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].rstrip("\n\r")
        if stripped and stripped[0].isalpha() and not stripped.startswith(" "):
            return idx
    return len(lines)


def top_list_item_starts(lines: list[str], start: int, end: int) -> list[int]:
    """
    Line indexes of the canonical-indent ``- `` items in a block body.

    The first dash indent seen is canonical; deeper dashes are inner
    action lists inside an item body and are skipped.
    """
    item_indent: str | None = None
    item_starts: list[int] = []
    for idx in range(start + 1, end):
        raw = lines[idx].rstrip("\n\r")
        stripped = raw.lstrip(" ")
        # A bare dash opens an item whose mapping starts on the next
        # line (the shape the nested-delete prune writes).
        if not stripped.startswith("- ") and stripped.rstrip(" ") != "-":
            continue
        prefix = raw[: len(raw) - len(stripped)]
        if item_indent is None:
            item_indent = prefix
        if prefix != item_indent:
            continue
        item_starts.append(idx)
    return item_starts


def leading_ws(line: str) -> str:
    """Leading spaces of *line* (YAML indentation is spaces-only; a tab counts as content)."""
    return line[: len(line) - len(line.lstrip(" "))]
