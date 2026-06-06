"""Pure line/diff layout utilities for the automation writer."""

from __future__ import annotations

from ...helpers.api import CommandError
from ...models.api import ErrorCode
from ...models.automations import YamlDiff


def _indent_block(block_text: str, indent: str) -> list[str]:
    """Prefix every non-empty line of *block_text* with *indent*."""
    out: list[str] = []
    for line in block_text.splitlines():
        if not line:
            out.append("")
            continue
        out.append(indent + line)
    return out


def _indent_for_top_list(rendered_item: str) -> str:
    """Indent *rendered_item* (one ``- ...`` block) for top-level list use."""
    # ``dump([item])`` already produces the dashed list form; we
    # use it as-is. The block is left at column-0 so it lands
    # correctly under any top-level domain.
    if not rendered_item.endswith("\n"):
        rendered_item += "\n"
    return rendered_item


def _locate_top_list_item(  # noqa: C901
    lines: list[str],
    domain: str,
    index: int,
) -> tuple[int, int]:
    """Return the line range of the *index*'th item under ``<domain>:``."""
    domain_start: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.rstrip("\n\r")
        if stripped == f"{domain}:" or stripped.startswith(f"{domain}:"):
            domain_start = idx
            break
    if domain_start is None:
        msg = f"Block {domain!r} not present"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    domain_end = len(lines)
    for idx in range(domain_start + 1, len(lines)):
        stripped = lines[idx].rstrip("\n\r")
        if stripped and stripped[0].isalpha() and not stripped.startswith(" "):
            domain_end = idx
            break
    # Only column-2 dashes count as top-level list items; deeper
    # dashes belong to nested action lists inside the item body.
    item_indent: str | None = None
    item_starts: list[int] = []
    for idx in range(domain_start + 1, domain_end):
        raw = lines[idx].rstrip("\n\r")
        stripped = raw.lstrip(" ")
        if not stripped.startswith("- "):
            continue
        prefix = raw[: len(raw) - len(stripped)]
        if item_indent is None:
            item_indent = prefix
        if prefix != item_indent:
            continue
        item_starts.append(idx)
    if index < 0 or index >= len(item_starts):
        msg = f"{domain}[{index}] out of range (have {len(item_starts)})"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    start = item_starts[index]
    end = item_starts[index + 1] if index + 1 < len(item_starts) else domain_end
    return start, end


def _locate_singleton_block(
    lines: list[str],
    block_key: str,
) -> tuple[int, int, str] | None:
    """Return ``(start, end, child_indent)`` for a singleton mapping block."""
    header = f"{block_key}:"
    start: int | None = None
    indent = "  "
    for idx, line in enumerate(lines):
        stripped = line.rstrip("\n\r")
        if stripped == header or stripped.startswith(header + " "):
            start = idx
            break
    if start is None:
        return None
    end = len(lines)
    captured = False
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].rstrip("\n\r")
        if not stripped:
            continue
        if not stripped.startswith(" "):
            if stripped[0].isalpha():
                end = idx
                break
            # Column-0 comment ends the block only when the next
            # non-blank line is also column-0 (a section banner
            # between two top-level blocks). A comment sitting
            # between a parent key and an indented child below is
            # a no-op — keep scanning.
            if _next_non_blank_at_col_zero(lines, idx + 1):
                end = idx
                break
            continue
        if not captured:
            indent = " " * (len(stripped) - len(stripped.lstrip(" ")))
            captured = True
    return start, end, indent


def _next_non_blank_at_col_zero(lines: list[str], start: int) -> bool:
    """Return True iff the next non-blank line at *start* or later sits at column 0."""
    for idx in range(start, len(lines)):
        stripped = lines[idx].rstrip("\n\r")
        if not stripped:
            continue
        return not stripped.startswith(" ")
    return False


def _build_diff_for_append(old_yaml: str, new_yaml: str) -> YamlDiff:
    """
    Build a diff describing the lines changed by an append-style write.

    Bounds the change to the region between the common leading and
    trailing lines, so a splice into a block that isn't the last in
    the file replaces only the changed span — without the suffix
    match the unchanged tail would be re-emitted and duplicated.
    """
    old_lines = old_yaml.splitlines()
    new_lines = new_yaml.splitlines()
    prefix = 0
    while (
        prefix < len(old_lines)
        and prefix < len(new_lines)
        and old_lines[prefix] == new_lines[prefix]
    ):
        prefix += 1
    suffix = 0
    while (
        suffix < len(old_lines) - prefix
        and suffix < len(new_lines) - prefix
        and old_lines[len(old_lines) - 1 - suffix] == new_lines[len(new_lines) - 1 - suffix]
    ):
        suffix += 1
    from_line = prefix + 1
    to_line = len(old_lines) - suffix  # == from_line - 1 ⇒ pure insert
    replacement = "\n".join(new_lines[prefix : len(new_lines) - suffix])
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    return YamlDiff(fromLine=from_line, toLine=to_line, replacement=replacement)
