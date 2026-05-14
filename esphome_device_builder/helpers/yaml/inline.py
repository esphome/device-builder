"""Inline-handler edits: upsert / remove ``on_*:`` blocks under configured components."""

from __future__ import annotations

import re

from .scalar import ESPHOME_YAML_INDENT


def upsert_inline_handler(
    yaml_text: str,
    *,
    component_domain: str,
    component_id: str,
    handler_key: str,
    rendered_yaml: str,
) -> tuple[str, int, int] | None:
    """
    Insert or replace ``<handler_key>:`` inline under a configured component.

    Used by the automation writer for inline ``on_*:`` triggers under
    component instances (``binary_sensor[i].on_press``, ``light[i].on_turn_on``,
    ...) and for ``effects:`` entries under a light. Returns
    ``(new_yaml_text, from_line, to_line)`` matching the
    :class:`automations.YamlDiff` convention — ``from_line <= to_line``
    for a replace, ``to_line == from_line - 1`` for a pure insert.
    ``None`` when the component instance can't be located (no
    ``id:`` match under ``<component_domain>:``).

    Adjacent siblings are preserved: this only touches the lines
    spanning ``<handler_key>:`` and its indented children. The
    *rendered_yaml* string is emitted at the same indent as the
    sibling fields.
    """
    lines = yaml_text.splitlines(keepends=True)
    span = _locate_component_instance(lines, component_domain, component_id)
    if span is None:
        return None
    instance_start, instance_end, child_indent = span

    # Look for an existing ``<handler_key>:`` line under this
    # instance. The key is at exactly ``child_indent`` columns of
    # leading whitespace.
    handler_re = re.compile(rf"^{re.escape(child_indent)}{re.escape(handler_key)}:\s*(?:#.*)?$")
    handler_start: int | None = None
    handler_end: int | None = None
    for idx in range(instance_start, instance_end):
        if handler_re.match(lines[idx].rstrip("\n\r")):
            handler_start = idx
            # Walk forward to find the first sibling-indented line
            # (or instance end).
            for jdx in range(idx + 1, instance_end):
                content = lines[jdx].rstrip("\n\r")
                if not content:
                    continue
                leading = len(content) - len(content.lstrip(" "))
                if leading <= len(child_indent):
                    handler_end = jdx
                    break
            if handler_end is None:
                handler_end = instance_end
            break

    rendered_lines = _indent_block(rendered_yaml, child_indent)
    rendered_text = "\n".join(rendered_lines) + "\n"

    if handler_start is not None and handler_end is not None:
        # Replace the existing handler block.
        new_lines = [*lines[:handler_start], rendered_text, *lines[handler_end:]]
        new_text = "".join(new_lines)
        return new_text, handler_start + 1, handler_end
    # Insert a new handler at the end of the instance, before any
    # trailing blank lines.
    insert_at = instance_end
    while insert_at > instance_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new_lines = [*lines[:insert_at], rendered_text, *lines[insert_at:]]
    new_text = "".join(new_lines)
    # Pure-insert: ``toLine == fromLine - 1`` flags the empty
    # replaced range. See :class:`automations.YamlDiff`.
    return new_text, insert_at + 1, insert_at


def remove_inline_handler(
    yaml_text: str,
    *,
    component_domain: str,
    component_id: str,
    handler_key: str,
) -> tuple[str, int, int] | None:
    """
    Delete an inline handler under a configured component.

    Returns ``(new_yaml_text, from_line, to_line)`` matching the
    same :class:`automations.YamlDiff` shape ``upsert_inline_handler``
    emits, or ``None`` when the handler isn't there.
    """
    lines = yaml_text.splitlines(keepends=True)
    span = _locate_component_instance(lines, component_domain, component_id)
    if span is None:
        return None
    instance_start, instance_end, child_indent = span
    handler_re = re.compile(rf"^{re.escape(child_indent)}{re.escape(handler_key)}:\s*(?:#.*)?$")
    for idx in range(instance_start, instance_end):
        if not handler_re.match(lines[idx].rstrip("\n\r")):
            continue
        handler_end = instance_end
        for jdx in range(idx + 1, instance_end):
            content = lines[jdx].rstrip("\n\r")
            if not content:
                continue
            leading = len(content) - len(content.lstrip(" "))
            if leading <= len(child_indent):
                handler_end = jdx
                break
        new_lines = [*lines[:idx], *lines[handler_end:]]
        return "".join(new_lines), idx + 1, handler_end
    return None


def _locate_component_instance(
    lines: list[str],
    domain: str,
    component_id: str,
) -> tuple[int, int, str] | None:
    """
    Find the line range of a specific ``- id: <component_id>`` block.

    Returns ``(start_idx, end_idx, child_indent)`` — dash-line
    index, one-past-last-line index, and the leading whitespace of
    the instance's child fields.
    """
    header_re = re.compile(rf"^{re.escape(domain)}:\s*(?:#.*)?$")
    domain_start: int | None = None
    for idx, line in enumerate(lines):
        if header_re.match(line.rstrip("\n\r")):
            domain_start = idx
            break
    if domain_start is None:
        return None
    domain_end = len(lines)
    for idx in range(domain_start + 1, len(lines)):
        stripped = lines[idx].rstrip("\n\r")
        if stripped and stripped[0].isalpha() and not stripped.startswith(" "):
            domain_end = idx
            break

    # Walk the domain body looking for a list item whose first child
    # line carries ``id: <component_id>``. Only column-2 dashes count
    # as instance starts — deeper dashes are inner action lists.
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
            # Inner action list — deeper indent than the canonical
            # list-of-instances. Skip.
            continue
        item_starts.append(idx)

    for run, start in enumerate(item_starts):
        end = item_starts[run + 1] if run + 1 < len(item_starts) else domain_end
        dash_indent = lines[start][: len(lines[start]) - len(lines[start].lstrip(" "))]
        child_indent = dash_indent + ESPHOME_YAML_INDENT
        if _instance_id_matches(lines, start, end, child_indent, component_id):
            return start, end, child_indent
    return None


def _instance_id_matches(
    lines: list[str],
    start: int,
    end: int,
    child_indent: str,
    component_id: str,
) -> bool:
    """
    Return True iff the instance at *start* carries ``id: component_id``.

    Two shapes the schema permits: ``- id: <comp_id>`` on the dash
    line itself, or ``id:`` as a regular child field at
    ``child_indent`` on a later line.
    """
    first_line = lines[start].rstrip("\n\r")
    inline_match = re.match(r"^\s*-\s*id:\s*(?P<id>\S+)", first_line)
    if inline_match:
        return inline_match.group("id") == component_id
    child_re = re.compile(rf"^{re.escape(child_indent)}id:\s*(?P<id>\S+)")
    for jdx in range(start, end):
        m = child_re.match(lines[jdx].rstrip("\n\r"))
        if m:
            return m.group("id") == component_id
    return False


def _indent_block(block_text: str, indent: str) -> list[str]:
    """Return *block_text* with every non-empty line prefixed by *indent*."""
    out: list[str] = []
    for line in block_text.splitlines():
        if not line:
            out.append("")
            continue
        out.append(indent + line)
    return out
