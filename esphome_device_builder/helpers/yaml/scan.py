"""Shared line-scan primitives for the YAML text-splice helpers."""

from __future__ import annotations

import re

# Regex prefix for a key on its list item's dash line (``- <key>: ...``).
DASH_KEY_PREFIX = r"^\s*-\s+"


def is_ignored_top_level_key(key: str) -> bool:
    """Report whether esphome ignores this top-level key (dot-prefixed anchor container)."""
    return key.startswith(".")


def opens_top_level_block(stripped: str) -> bool:
    """Report whether a rstripped line opens a column-0 block; comments deliberately don't."""
    return bool(stripped) and (stripped[0].isalpha() or stripped[0] == ".")


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


#: Generic column-0 form of ``key_header_re`` — keep the two shapes in sync.
_TOP_LEVEL_HEADER_RE = re.compile(r"^(\S+):\s*(?:#.*)?$")


def top_level_key_index(lines: list[str]) -> dict[str, int]:
    """First-occurrence index of every column-0 ``<key>:`` header line."""
    index: dict[str, int] = {}
    for idx, line in enumerate(lines):
        match = _TOP_LEVEL_HEADER_RE.match(line.rstrip("\n\r"))
        if match is not None:
            index.setdefault(match.group(1), idx)
    return index


def block_end_index(lines: list[str], start: int) -> int:
    """First line after *start* that opens the next top-level block; ``len(lines)`` at EOF."""
    for idx in range(start + 1, len(lines)):
        if opens_top_level_block(lines[idx].rstrip("\n\r")):
            return idx
    return len(lines)


def is_list_item_line(stripped: str) -> bool:
    """Report whether a whitespace-stripped line opens a list item (``- foo`` or lone ``-``)."""
    return stripped.startswith("- ") or stripped.rstrip(" ") == "-"


def child_block_end(lines: list[str], start: int, end_bound: int, indent: str) -> int:
    """
    End (exclusive) of the child block whose key line sits at *indent*.

    A flush-style list item at the key's own indent continues the block;
    blank and comment lines never end it. The end trims back over
    trailing blanks and comments at or above the key's indent (they
    belong to the gap before the next key); deeper trailing comments
    stay inside the block.
    """
    end = end_bound
    for idx in range(start + 1, end_bound):
        content = lines[idx].rstrip("\n\r")
        stripped = content.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        leading = len(content) - len(stripped)
        if leading < len(indent) or (leading == len(indent) and not is_list_item_line(stripped)):
            end = idx
            break
    while end - 1 > start:
        content = lines[end - 1].rstrip("\n\r")
        stripped = content.lstrip(" ")
        if stripped and (
            not stripped.startswith("#") or len(content) - len(stripped) > len(indent)
        ):
            break
        end -= 1
    return end


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
        if not is_list_item_line(stripped):
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


def normalize_trailing_newline(yaml_text: str) -> tuple[str, str]:
    """
    Return ``(text, nl)``: the text's newline style, with a bare final line ended.

    ``splitlines(keepends=True)`` leaves the last element bare, so an
    end-of-file insert would otherwise splice onto it.
    """
    nl = "\r\n" if "\r\n" in yaml_text else "\n"
    if yaml_text and not yaml_text.endswith(("\n", "\r")):
        yaml_text += nl
    return yaml_text, nl


def trim_trailing_blanks(lines: list[str], block_start: int, insert_at: int) -> int:
    """Back *insert_at* over blank lines so an insert lands after the last content line."""
    while insert_at > block_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    return insert_at
