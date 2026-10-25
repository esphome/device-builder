"""Merge a board's demo.yaml overlay into generated device YAML."""

from __future__ import annotations

import re

from .component import (
    _append_block,
    _find_top_level_block_bounds,
    _line_defined_id,
    _list_item_indent,
    _splice_into_domain_block,
)
from .scalar import ESPHOME_YAML_INDENT, block_body_is_list
from .scan import leading_ws, opens_top_level_block, trim_trailing_gap

_CHILD_KEY_RE = re.compile(r"([A-Za-z0-9_.-]+):(?:\s|$)")
_EXTEND_ITEM_RE = re.compile(r"-\s+id:\s+!extend\s+(\S+)(?:\s+#.*)?")
_OVERLAY_HEADER_RE = re.compile(r"([^\s:#][^\s:]*):\s*(?:#.*)?")


def merge_yaml_overlay(existing: str, overlay: str) -> str:
    """
    Merge every top-level block of *overlay* into *existing* YAML.

    A block absent from *existing* is appended; a present one gains the
    overlay's list items or mapping keys, and a ``- id: !extend <id>`` item
    adds its keys to the existing item with that id. Raises ``ValueError``
    on a repeated key, a missing ``!extend`` target, or list vs mapping form.
    """
    blocks, trailing = _overlay_blocks(overlay)
    merged = existing
    for key, comments, raw_body in blocks:
        merged, body = _apply_extensions(merged, key, raw_body)
        lines = merged.splitlines(keepends=True)
        bounds = _find_top_level_block_bounds(lines, key)
        if bounds is None:
            merged = _append_block(merged, "\n".join([*comments, f"{key}:", *body]))
            continue
        if not any(line.strip() for line in body):
            if comments:
                header, end = bounds
                indent = _body_indent(lines, header, end)
                merged = _insert_lines(lines, end, indent, comments, "")
            continue
        overlay_lines = [f"{key}:", *body]
        is_list = block_body_is_list(lines, *bounds)
        if is_list != block_body_is_list(overlay_lines, 0, len(overlay_lines)):
            raise ValueError(f"overlay {key}: list and mapping forms differ")
        if not is_list:
            if repeated := _child_keys(lines, *bounds) & _child_keys(overlay_lines):
                raise ValueError(f"overlay {key}: repeats {', '.join(sorted(repeated))}")
            header, end = bounds
            child = _body_indent(lines, header, end)
            src = next((leading_ws(line) for line in body if line.strip()), "")
            notes = [src + line.lstrip() for line in comments]
            merged = _insert_lines(lines, end, child, [*notes, *body], src)
            continue
        indent = next((leading_ws(line) for line in body if line.strip()), ESPHOME_YAML_INDENT)
        block = [f"{key}:", *(indent + line.lstrip() for line in comments), *body]
        spliced = _splice_into_domain_block(merged, key, "\n".join(block))
        assert spliced is not None  # the block's bounds were found above
        merged = spliced
    return _append_block(merged, "\n".join(trailing)) if trailing else merged


def _overlay_blocks(text: str) -> tuple[list[tuple[str, list[str], list[str]]], list[str]]:
    """
    Split *text* into ``(key, leading comments, body lines)`` blocks plus trailing comments.

    Raises ``ValueError`` on a repeated key, a header with an inline value, or
    anything but comments before the first block.
    """
    lines = text.splitlines()
    headers: list[tuple[str, int]] = []
    for idx, line in enumerate(lines):
        if not opens_top_level_block(line.rstrip()):
            continue
        match = _OVERLAY_HEADER_RE.fullmatch(line.rstrip())
        if match is None:
            raise ValueError(f"overlay line {idx + 1}: top-level value must be a block")
        if match.group(1) in {key for key, _ in headers}:
            raise ValueError(f"overlay {match.group(1)}: repeated top-level block")
        headers.append((match.group(1), idx))
    first = headers[0][1] if headers else len(lines)
    if any(line.strip() and not line.lstrip().startswith("#") for line in lines[:first]):
        raise ValueError("overlay: only comments may precede the first block")
    blocks: list[tuple[str, list[str], list[str]]] = []
    previous_end = 0
    for pos, (key, start) in enumerate(headers):
        end = headers[pos + 1][1] if pos + 1 < len(headers) else len(lines)
        content_end = trim_trailing_gap(lines, start, end)
        comments = [line for line in lines[previous_end:start] if line.strip()]
        blocks.append((key, comments, lines[start + 1 : content_end]))
        previous_end = content_end
    return blocks, [line for line in lines[previous_end:] if line.strip()]


def _body_indent(lines: list[str], header_idx: int, end_idx: int) -> str:
    """Indent of a block's first body line; the canonical indent for an empty body."""
    return next(
        (leading_ws(line) for line in lines[header_idx + 1 : end_idx] if line.strip()),
        ESPHOME_YAML_INDENT,
    )


def _child_keys(lines: list[str], header_idx: int = 0, end_idx: int | None = None) -> set[str]:
    """Keys of a mapping block's direct children, lines ``(header_idx, end_idx)``."""
    body = lines[header_idx + 1 : len(lines) if end_idx is None else end_idx]
    indent = next((leading_ws(line) for line in body if line.strip()), "")
    keys: set[str] = set()
    for line in body:
        if leading_ws(line) == indent and (match := _CHILD_KEY_RE.match(line.strip())):
            keys.add(match.group(1))
    return keys


def _apply_extensions(existing: str, key: str, body: list[str]) -> tuple[str, list[str]]:
    """Fold *body*'s ``!extend`` items into *existing*; return it and the remaining body."""
    kept: list[str] = []
    for item in _split_list_items(body):
        dash_line = next((line for line in item if line.strip().startswith("-")), "")
        match = _EXTEND_ITEM_RE.fullmatch(dash_line.strip())
        if match is None:
            if any("!extend" in line for line in item):
                raise ValueError(f"overlay {key}: write !extend as '- id: !extend <id>'")
            kept.extend(item)
            continue
        extra = item[item.index(dash_line) + 1 :]
        existing = _extend_item(existing, key, match.group(1), extra)
    return existing, kept


def _split_list_items(body: list[str]) -> list[list[str]]:
    """Split a list block *body* into per-item line groups at its dash indent."""
    dash = next((leading_ws(line) for line in body if line.strip().startswith("-")), None)
    items: list[list[str]] = [[]]
    for line in body:
        opens_item = dash is not None and leading_ws(line) == dash and line.strip().startswith("-")
        if opens_item and any(entry.strip().startswith("-") for entry in items[-1]):
            items.append([])
        items[-1].append(line)
    return [item for item in items if item]


def _extend_item(existing: str, key: str, target: str, extra: list[str]) -> str:
    """Splice *extra* key lines into the ``<key>:`` list item whose id is *target*."""
    lines = existing.splitlines(keepends=True)
    bounds = _find_top_level_block_bounds(lines, key)
    if bounds is None or not block_body_is_list(lines, *bounds):
        raise ValueError(f"overlay {key}: !extend {target} has no list to extend")
    header, end = bounds
    dash = _list_item_indent(lines, header, end)
    starts = [
        idx
        for idx in range(header + 1, end)
        if leading_ws(lines[idx]) == dash and lines[idx].strip().startswith("-")
    ]
    for pos, start in enumerate(starts):
        stop = trim_trailing_gap(lines, start, starts[pos + 1] if pos + 1 < len(starts) else end)
        item = lines[start:stop]
        indent = next((leading_ws(line) for line in item[1:] if line.strip()), dash + "  ")
        own = [item[0].replace("-", " ", 1), *item[1:]]
        if target not in {_line_defined_id(line) for line in own if leading_ws(line) == indent}:
            continue
        if repeated := _child_keys(["", *own]) & _child_keys(["", *extra]):
            raise ValueError(
                f"overlay {key}: !extend {target} repeats {', '.join(sorted(repeated))}"
            )
        src = next((leading_ws(line) for line in extra if line.strip()), indent)
        return _insert_lines(lines, stop, indent, extra, src)
    raise ValueError(f"overlay {key}: !extend {target} matches no item")


def _insert_lines(lines: list[str], stop: int, indent: str, extra: list[str], src: str) -> str:
    """Insert *extra* before ``lines[stop]``, re-indented from *src* to *indent*."""
    moved = [indent + line[len(src) :] if line.strip() else line for line in extra]
    insertion = "".join(line.rstrip("\n") + "\n" for line in moved)
    before = "".join(lines[:stop])
    if not before.endswith("\n"):
        before += "\n"
    return before + insertion + "".join(lines[stop:])
