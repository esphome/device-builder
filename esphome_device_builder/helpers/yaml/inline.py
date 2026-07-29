"""Inline-handler edits: upsert / remove ``on_*:`` blocks under configured components."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .scalar import ESPHOME_YAML_INDENT, YamlUpsertNotSupportedError, block_body_is_list
from .scan import (
    block_end_index,
    find_block_header,
    key_header_re,
    leading_ws,
    top_list_item_starts,
)


@dataclass(frozen=True, slots=True)
class SubEntityRef:
    """Address of a nested sub-entity block: parent instance + sub-block key."""

    parent_domain: str
    parent_id: str
    sub_key: str


def synthetic_instance_index(domain: str, component_id: str) -> int | None:
    """
    Decode a parser-synthesized ``<domain>_<idx>`` id back to its list index.

    The automation parser labels an id-less component instance
    ``f"{domain}_{idx}"`` — its position in the ``<domain>:`` list. id-less
    instances are valid ESPHome (a GPIO ``binary_sensor`` with no ``id:``), so
    their inline ``on_*:`` handlers must still resolve on write. Returns
    ``None`` for a real id (which the literal ``id:`` lookup handles).
    """
    prefix = f"{domain}_"
    if not component_id.startswith(prefix):
        return None
    suffix = component_id[len(prefix) :]
    return int(suffix) if suffix.isdecimal() else None


def upsert_inline_handler(
    yaml_text: str,
    *,
    component_domain: str,
    component_id: str,
    handler_key: str,
    rendered_yaml: str,
) -> tuple[str, int, int, str] | None:
    """
    Insert or replace ``<handler_key>:`` inline under a configured component.

    Used by the automation writer for inline ``on_*:`` triggers under
    component instances (``binary_sensor[i].on_press``, ``light[i].on_turn_on``,
    ...) and for ``effects:`` entries under a light. Returns
    ``(new_yaml_text, from_line, to_line, replacement)``:

      * ``from_line`` / ``to_line`` match the
        :class:`automations.YamlDiff` convention — ``from_line <= to_line``
        for a replace (the OLD line range in the pre-splice YAML),
        ``to_line == from_line - 1`` for a pure insert.
      * ``replacement`` is the indented rendered text spliced into
        the YAML — callers feed it straight into ``YamlDiff.replacement``
        rather than re-deriving from the new YAML (which is broken for
        the pure-insert case because the slice ends up empty).

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
    return _apply_handler_upsert(lines, span, handler_key, rendered_yaml)


def upsert_subentity_handler(
    yaml_text: str,
    ref: SubEntityRef,
    *,
    handler_key: str,
    rendered_yaml: str,
) -> tuple[str, int, int, str] | None:
    """
    Insert or replace ``<handler_key>:`` under a nested sub-entity block.

    Splices at *ref*'s ``<sub_key>:`` block child indent inside its parent
    instance. Same return shape as :func:`upsert_inline_handler`; ``None``
    when the sub-block isn't found.
    """
    lines = yaml_text.splitlines(keepends=True)
    span = _locate_subentity_instance(lines, ref)
    if span is None:
        return None
    return _apply_handler_upsert(lines, span, handler_key, rendered_yaml)


def upsert_nested_handler(
    yaml_text: str,
    *,
    component_domain: str,
    component_id: str,
    field_segments: Sequence[str],
    rendered_yaml: str,
) -> tuple[str, int, int, str] | None:
    """
    Insert or replace a nested field block addressed by *field_segments*.

    Segments are mapping keys, a decimal segment indexing a YAML list
    (``valves.0.run_duration_number.set_action``). Missing intermediate
    *mappings* are created around the rendered leaf; a missing or
    out-of-range list item is refused (``None``) — never fabricated.
    Same return shape as :func:`upsert_inline_handler`; raises
    :class:`YamlUpsertNotSupportedError` on an inline-scalar intermediate.
    """
    lines = yaml_text.splitlines(keepends=True)
    span = _locate_component_instance(lines, component_domain, component_id)
    if span is None:
        return None
    intermediates = list(field_segments[:-1])
    leaf_key = field_segments[-1]
    stack, consumed, list_miss = _descend_field_segments(
        lines, _instance_frame(lines, span), intermediates
    )
    if list_miss:
        return None
    target = (stack[-1].start, stack[-1].end, stack[-1].child_indent)
    if consumed == len(intermediates):
        return _apply_handler_upsert(lines, target, leaf_key, rendered_yaml)
    remaining = intermediates[consumed:]
    if any(seg.isdecimal() for seg in remaining):
        return None
    rendered = rendered_yaml
    for seg in reversed(remaining):
        rendered = f"{seg}:\n" + "\n".join(_indent_block(rendered, ESPHOME_YAML_INDENT))
    return _apply_handler_upsert(lines, target, remaining[0], rendered)


def remove_nested_handler(
    yaml_text: str,
    *,
    component_domain: str,
    component_id: str,
    field_segments: Sequence[str],
) -> tuple[str, int, int] | None:
    """
    Delete a nested field block addressed by *field_segments*.

    Inverse of :func:`upsert_nested_handler`; also prunes intermediate
    mappings the removal empties (never a list item or the instance).
    ``None`` when any path step or the leaf is absent.
    """
    lines = yaml_text.splitlines(keepends=True)
    span = _locate_component_instance(lines, component_domain, component_id)
    if span is None:
        return None
    intermediates = list(field_segments[:-1])
    leaf_key = field_segments[-1]
    try:
        stack, consumed, _list_miss = _descend_field_segments(
            lines, _instance_frame(lines, span), intermediates
        )
    except YamlUpsertNotSupportedError:
        return None
    if consumed != len(intermediates):
        return None
    target = (stack[-1].start, stack[-1].end, stack[-1].child_indent)
    removed = _apply_handler_remove(lines, target, leaf_key)
    if removed is None:
        return None
    _new_text, from_line, to_line = removed
    rm_start, rm_end = from_line - 1, to_line
    # Prune enclosing mappings the removal leaves empty. List items and
    # the instance span (stack[0]) are never pruned — deleting a valve
    # item would shift sibling indices other parsed locations hold.
    for frame in reversed(stack[1:]):
        if frame.is_list_item:
            break
        emptied = all(
            not lines[idx].strip()
            for idx in range(frame.start + 1, frame.end)
            if not (rm_start <= idx < rm_end)
        )
        if not emptied:
            break
        rm_start = frame.start
        rm_end = max(rm_end, frame.end)
    new_lines = [*lines[:rm_start], *lines[rm_end:]]
    return "".join(new_lines), rm_start + 1, rm_end


@dataclass(frozen=True, slots=True)
class _SpanFrame:
    """One resolved step of a nested descent: block bounds + child indent."""

    start: int
    end: int
    child_indent: str
    is_list_item: bool = False


def _instance_frame(lines: list[str], span: tuple[int, int, str]) -> _SpanFrame:
    """Frame for a located instance; a dash-line start marks it a list item."""
    start, end, child_indent = span
    is_item = lines[start].lstrip().startswith("- ")
    return _SpanFrame(start, end, child_indent, is_list_item=is_item)


def _descend_field_segments(
    lines: list[str],
    frame: _SpanFrame,
    segments: Sequence[str],
) -> tuple[list[_SpanFrame], int, bool]:
    """
    Resolve *segments* stepwise from *frame*, deepest-first frames on a stack.

    Returns ``(stack, consumed, list_miss)`` — the frames located (the
    instance frame first), how many segments resolved, and whether the
    walk stopped at a missing/out-of-range list index. Raises
    :class:`YamlUpsertNotSupportedError` when a key segment exists with
    an inline scalar value (no block to descend into).
    """
    stack = [frame]
    consumed = 0
    for seg in segments:
        frame = stack[-1]
        if seg.isdecimal():
            if not block_body_is_list(lines, frame.start, frame.end):
                return stack, consumed, True
            items = top_list_item_starts(lines, frame.start, frame.end)
            idx = int(seg)
            if idx >= len(items):
                return stack, consumed, True
            start = items[idx]
            end = items[idx + 1] if idx + 1 < len(items) else frame.end
            stack.append(_SpanFrame(start, end, _child_indent(lines, start), is_list_item=True))
            consumed += 1
            continue
        located = _locate_key_block(lines, frame, seg)
        if located is None:
            return stack, consumed, False
        stack.append(located)
        consumed += 1
    return stack, consumed, False


def _locate_key_block(lines: list[str], frame: _SpanFrame, key: str) -> _SpanFrame | None:
    """
    Find ``<key>:`` as a block header inside *frame*, or ``None`` when absent.

    Matches the plain child-indent form and — for a list-item frame —
    the dash-line first-key form (``- <key>:``). A ``<key>: <scalar>``
    line raises :class:`YamlUpsertNotSupportedError` instead of
    reporting the key missing, so an upsert can't shadow it.
    """
    header_re = key_header_re(key, indent=frame.child_indent)
    dash_re = re.compile(rf"^\s*-\s+{re.escape(key)}:\s*(?:#.*)?$")
    child_scalar_re = re.compile(rf"^{re.escape(frame.child_indent)}{re.escape(key)}:\s*[^\s#]")
    dash_scalar_re = re.compile(rf"^\s*-\s+{re.escape(key)}:\s*[^\s#]")
    inline_msg = f"{key!r} has an inline value; rewrite it as a block mapping first"
    start: int | None = None
    for idx in range(frame.start, frame.end):
        content = lines[idx].rstrip("\n\r")
        if header_re.match(content):
            start = idx
            break
        if idx == frame.start and frame.is_list_item:
            if dash_re.match(content):
                start = idx
                break
            if dash_scalar_re.match(content):
                raise YamlUpsertNotSupportedError(inline_msg)
        if child_scalar_re.match(content):
            raise YamlUpsertNotSupportedError(inline_msg)
    if start is None:
        return None
    end = _block_end(lines, start, frame.end, frame.child_indent)
    child = frame.child_indent + ESPHOME_YAML_INDENT
    for idx in range(start + 1, end):
        content = lines[idx].rstrip("\n\r")
        if content:
            child = leading_ws(content)
            break
    return _SpanFrame(start, end, child)


def _block_end(lines: list[str], start: int, end_bound: int, indent: str) -> int:
    """First line index after *start* whose indent is <= len(*indent*); *end_bound* if none."""
    for idx in range(start + 1, end_bound):
        content = lines[idx].rstrip("\n\r")
        if not content:
            continue
        if len(content) - len(content.lstrip(" ")) <= len(indent):
            return idx
    return end_bound


def _apply_handler_upsert(
    lines: list[str],
    span: tuple[int, int, str],
    handler_key: str,
    rendered_yaml: str,
) -> tuple[str, int, int, str]:
    """Splice ``<handler_key>:`` into the located *span*; the shared upsert tail."""
    instance_start, instance_end, child_indent = span

    # Look for an existing ``<handler_key>:`` line under this
    # instance. The key is at exactly ``child_indent`` columns of
    # leading whitespace.
    handler_re = key_header_re(handler_key, indent=child_indent)
    handler_start: int | None = None
    handler_end: int | None = None
    for idx in range(instance_start, instance_end):
        if handler_re.match(lines[idx].rstrip("\n\r")):
            handler_start = idx
            handler_end = _block_end(lines, idx, instance_end, child_indent)
            break

    rendered_lines = _indent_block(rendered_yaml, child_indent)
    rendered_text = "\n".join(rendered_lines) + "\n"

    if handler_start is not None and handler_end is not None:
        # Replace the existing handler block.
        new_lines = [*lines[:handler_start], rendered_text, *lines[handler_end:]]
        new_text = "".join(new_lines)
        return new_text, handler_start + 1, handler_end, rendered_text
    # Insert a new handler at the end of the instance, before any
    # trailing blank lines.
    insert_at = instance_end
    while insert_at > instance_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new_lines = [*lines[:insert_at], rendered_text, *lines[insert_at:]]
    new_text = "".join(new_lines)
    # Pure-insert: ``toLine == fromLine - 1`` flags the empty
    # replaced range. See :class:`automations.YamlDiff`.
    return new_text, insert_at + 1, insert_at, rendered_text


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
    return _apply_handler_remove(lines, span, handler_key)


def remove_subentity_handler(
    yaml_text: str,
    ref: SubEntityRef,
    *,
    handler_key: str,
) -> tuple[str, int, int] | None:
    """Delete ``<handler_key>:`` from a nested sub-entity block (inverse of upsert)."""
    lines = yaml_text.splitlines(keepends=True)
    span = _locate_subentity_instance(lines, ref)
    if span is None:
        return None
    return _apply_handler_remove(lines, span, handler_key)


def _apply_handler_remove(
    lines: list[str],
    span: tuple[int, int, str],
    handler_key: str,
) -> tuple[str, int, int] | None:
    """Drop ``<handler_key>:`` from the located *span*; the shared remove tail."""
    instance_start, instance_end, child_indent = span
    handler_re = key_header_re(handler_key, indent=child_indent)
    for idx in range(instance_start, instance_end):
        if not handler_re.match(lines[idx].rstrip("\n\r")):
            continue
        handler_end = _block_end(lines, idx, instance_end, child_indent)
        new_lines = [*lines[:idx], *lines[handler_end:]]
        return "".join(new_lines), idx + 1, handler_end
    return None


def _locate_subentity_instance(
    lines: list[str],
    ref: SubEntityRef,
) -> tuple[int, int, str] | None:
    """
    Find the line range of *ref*'s ``<sub_key>:`` block inside its parent instance.

    Locates the parent instance, then the ``<sub_key>:`` header among its
    child fields. Returns ``(start, end, child_indent)`` where *start* is the
    header line, *end* one past the sub-block's last line, and *child_indent*
    the leading whitespace of the sub-block's own fields (where a sibling
    handler is spliced). ``None`` when the parent or the sub-block isn't
    present.
    """
    span = _locate_component_instance(lines, ref.parent_domain, ref.parent_id)
    if span is None:
        return None
    instance_start, instance_end, parent_child_indent = span
    header_re = key_header_re(ref.sub_key, indent=parent_child_indent)
    sub_start: int | None = None
    for idx in range(instance_start, instance_end):
        if header_re.match(lines[idx].rstrip("\n\r")):
            sub_start = idx
            break
    if sub_start is None:
        return None
    sub_end = _block_end(lines, sub_start, instance_end, parent_child_indent)
    # Child indent = the first non-blank child's leading whitespace; for an
    # empty sub-block (no fields yet) splice one indent level in.
    sub_child_indent = parent_child_indent + ESPHOME_YAML_INDENT
    for idx in range(sub_start + 1, sub_end):
        content = lines[idx].rstrip("\n\r")
        if content:
            sub_child_indent = leading_ws(content)
            break
    return sub_start, sub_end, sub_child_indent


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
    domain_start = find_block_header(lines, domain)
    if domain_start is None:
        return None
    domain_end = block_end_index(lines, domain_start)

    if not block_body_is_list(lines, domain_start, domain_end):
        # Flat singleton mapping (``sun:`` / ``mqtt:``) — its only ``- ``
        # lines (if any) are inner action lists, never instance items.
        return _locate_singleton_instance(lines, domain, component_id, domain_start, domain_end)

    # Walk the domain body looking for a list item whose first child
    # line carries ``id: <component_id>``.
    item_starts = top_list_item_starts(lines, domain_start, domain_end)
    bounds = _instance_bounds(lines, item_starts, domain_end)
    for start, end, child_indent in bounds:
        if _instance_declared_id(lines, start, end, child_indent) == component_id:
            return start, end, child_indent

    return _locate_idless_instance(lines, domain, component_id, bounds)


def _instance_bounds(
    lines: list[str], item_starts: list[int], domain_end: int
) -> list[tuple[int, int, str]]:
    """Per-instance ``(start, end, child_indent)`` triples for the located items."""
    bounds: list[tuple[int, int, str]] = []
    for pos, start in enumerate(item_starts):
        end = item_starts[pos + 1] if pos + 1 < len(item_starts) else domain_end
        bounds.append((start, end, _child_indent(lines, start)))
    return bounds


def _child_indent(lines: list[str], start: int) -> str:
    """Leading whitespace of an instance's child fields (its dash indent plus one level)."""
    return leading_ws(lines[start]) + ESPHOME_YAML_INDENT


def _locate_idless_instance(
    lines: list[str],
    domain: str,
    component_id: str,
    bounds: list[tuple[int, int, str]],
) -> tuple[int, int, str] | None:
    """
    Resolve the parser's positional ``<domain>_<idx>`` label for an id-less instance.

    Only resolves onto a genuinely id-less instance — a real id always matches
    the literal lookup first, and a stale positional id pointing at an id'd
    instance is refused (``None``) so the caller raises a clean "not found".
    """
    idx = synthetic_instance_index(domain, component_id)
    if idx is None or idx >= len(bounds):
        return None
    start, end, child_indent = bounds[idx]
    if _instance_declared_id(lines, start, end, child_indent) is not None:
        return None
    return start, end, child_indent


def _locate_singleton_instance(
    lines: list[str],
    domain: str,
    component_id: str,
    domain_start: int,
    domain_end: int,
) -> tuple[int, int, str] | None:
    """
    Locate a flat singleton component block (``sun:`` / ``mqtt:``) as one span.

    The ``<domain>:`` header is the instance start, the next top-level
    key is the end, and child fields sit one indent in. Matches when
    *component_id* is the block's declared ``id:`` or — when id-less —
    the domain name itself.
    """
    declared = _instance_declared_id(lines, domain_start, domain_end, ESPHOME_YAML_INDENT)
    if component_id == declared or (declared is None and component_id == domain):
        return domain_start, domain_end, ESPHOME_YAML_INDENT
    return None


def _instance_declared_id(
    lines: list[str],
    start: int,
    end: int,
    child_indent: str,
) -> str | None:
    """
    Return the ``id:`` the instance at *start* declares, or ``None`` when id-less.

    Two shapes the schema permits: ``- id: <comp_id>`` on the dash
    line itself, or ``id:`` as a regular child field at
    ``child_indent`` on a later line.
    """
    first_line = lines[start].rstrip("\n\r")
    inline_match = re.match(r"^\s*-\s*id:\s*(?P<id>\S+)", first_line)
    if inline_match:
        return _unquote_id(inline_match.group("id"))
    child_re = re.compile(rf"^{re.escape(child_indent)}id:\s*(?P<id>\S+)")
    for jdx in range(start, end):
        m = child_re.match(lines[jdx].rstrip("\n\r"))
        if m:
            return _unquote_id(m.group("id"))
    return None


def _unquote_id(raw: str) -> str:
    """
    Strip a surrounding matching quote pair from a captured ``id:`` token.

    ESPHome identifiers never contain quote characters, so a matching
    surrounding pair is always wrapper syntax.
    """
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    return raw


def _indent_block(block_text: str, indent: str) -> list[str]:
    """Return *block_text* with every non-empty line prefixed by *indent*."""
    out: list[str] = []
    for line in block_text.splitlines():
        if not line:
            out.append("")
            continue
        out.append(indent + line)
    return out
