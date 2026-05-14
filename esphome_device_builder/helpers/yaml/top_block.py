"""Top-level block upsert: insert / rewrite ``block.leaf`` in YAML text."""

from __future__ import annotations

import re

from .scalar import (
    ESPHOME_YAML_INDENT,
    YamlUpsertNotSupportedError,
    _safe_yaml_scalar,
    read_yaml_scalar,
)
from .substitution import rewrite_name_or_substitution


def _locate_top_block(lines: list[str], block_key: str) -> tuple[int, int, str] | None:
    """
    Find the column-0 ``block_key:`` block; return ``(start, end, child_indent)``.

    None when the block isn't present. Raises
    :class:`YamlUpsertNotSupportedError` when the header line
    has an inline value (flow-style ``{…}`` or a tag like
    ``!include``) — the line-based walker can't safely edit
    those.

    Comment rules differ by side of the block. Outside (looking
    for the opener), column-0 ``#`` lines are file / inter-block
    headers and get skipped. Inside, a column-0 line — comment
    or content — terminates the block; column-0 comments visually
    belong to whatever's *next*, and treating them as
    block-internal lets a subsequent insert land between two
    indented children (the wizard's ``# Board:`` /
    ``# Definition:`` annotations were the trigger).
    """
    header_re = re.compile(rf"^{re.escape(block_key)}:\s*(?P<rest>.*)$")
    start: int | None = None
    end = len(lines)
    indent = ESPHOME_YAML_INDENT
    indent_captured = False
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n\r")
        if not stripped:
            continue
        if start is None:
            if stripped.lstrip().startswith("#"):
                continue
            m = header_re.match(stripped)
            if m is None:
                continue
            if m.group("rest").split("#", 1)[0].strip():
                raise YamlUpsertNotSupportedError(
                    f"{block_key}: uses an inline value or flow-style "
                    "mapping; the line-based upsert can't safely "
                    "edit it. Convert the block to multi-line "
                    f"style ({block_key}:\\n  …) and try again."
                )
            start = i
            continue
        if not stripped[0].isspace():
            end = i
            break
        if stripped.lstrip().startswith("#"):
            continue
        if not indent_captured:
            # Capture the literal leading-whitespace prefix (spaces, tabs, or
            # a mix) rather than rebuilding a spaces-only string of the same
            # column width — a tab-indented block needs the insert to land
            # at the same tab depth or it lands at column 0.
            indent = stripped[: len(stripped) - len(stripped.lstrip())]
            indent_captured = True
    if start is None:
        return None
    return start, end, indent


def _find_prepend_anchor(lines: list[str]) -> int:
    """Return the line index past leading YAML directives / ``---`` markers."""
    anchor = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith(("%", "---")):
            return anchor
        anchor = i + 1
    return anchor


def upsert_yaml_leaf_under_top_block(
    yaml_text: str,
    block_key: str,
    leaf_key: str,
    new_value: str,
) -> str:
    r"""
    Set or insert ``block_key.leaf_key`` to *new_value* in *yaml_text*.

    Three behaviours, picked by the YAML's existing shape:

    1. **Leaf exists** at ``(block_key, leaf_key)`` — rewrite via
       :func:`rewrite_name_or_substitution` so the substitution-
       redirect / safe-quoting machinery applies.
    2. **Top-level ``block_key:`` exists but no ``leaf_key:``
       child** — insert ``  leaf_key: <value>`` at the end of the
       block body, matching the indent of any existing sibling
       (defaults to two spaces when the block has no children).
    3. **No ``block_key:`` block at all** — prepend a new
       ``block_key:\n  leaf_key: <value>\n`` block. Anchored
       below any leading YAML directives / ``---`` markers so
       the doc still parses. Used for package-driven configs
       where the ``esphome:`` block lives in an ``!include``d
       file; ESPHome's package merge gives our local leaf
       precedence over the package's.

    *new_value* is rendered through :func:`_safe_yaml_scalar` so
    YAML-special characters (``Bedroom #2`` etc.) round-trip
    safely. Caller passes the unquoted user input.
    """
    leaf_path = (block_key, leaf_key)
    if read_yaml_scalar(yaml_text, leaf_path) is not None:
        return rewrite_name_or_substitution(yaml_text, leaf_path, new_value)

    rendered = _safe_yaml_scalar(new_value)
    lines = yaml_text.splitlines(keepends=True)
    located = _locate_top_block(lines, block_key)

    if located is None:
        anchor = _find_prepend_anchor(lines)
        prefix = "".join(lines[:anchor])
        rest = "".join(lines[anchor:])
        sep = "" if not rest or rest.startswith("\n") else "\n"
        new_block = f"{block_key}:\n{ESPHOME_YAML_INDENT}{leaf_key}: {rendered}\n{sep}"
        return f"{prefix}{new_block}{rest}"

    block_start, block_end, indent = located
    # Trim trailing blank lines so the insert lands right after
    # the block's last content line, not after the visual gap.
    insert_at = block_end
    while insert_at > block_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new_line = f"{indent}{leaf_key}: {rendered}\n"
    return "".join([*lines[:insert_at], new_line, *lines[insert_at:]])
