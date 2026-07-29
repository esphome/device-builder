"""
Whole-file respell of legacy renamed-key spellings to canonical.

One rule function per rename shape, folded by ``render_canonicalize``;
every rule is a pure, line-for-line ``lines -> lines`` transform so
comments and formatting survive. A future rename shape (a platform
section field, say) adds a rule function and joins the fold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...models.automations import YamlDiff
from . import api_actions
from .writing_layout import _locate_singleton_block


@dataclass(frozen=True)
class ActionNodeRename:
    """A registry action's legacy/canonical node ids and body field."""

    legacy_id: str
    canonical_id: str
    legacy_field: str
    canonical_field: str


_ACTION_NODE_RENAMES = (
    ActionNodeRename("homeassistant.service", "homeassistant.action", "service", "action"),
)


def render_canonicalize(yaml_text: str) -> tuple[str, YamlDiff] | None:
    """
    Respell every legacy spelling in *yaml_text* to canonical.

    Returns ``(new_text, diff)`` with one contiguous splice covering the
    first through last changed line, or ``None`` when nothing is legacy.
    """
    lines = yaml_text.splitlines(keepends=True)
    out = _canonicalize_api_actions(list(lines))
    out = _canonicalize_action_nodes(out)
    if out == lines:
        return None
    changed = [idx for idx in range(len(lines)) if lines[idx] != out[idx]]
    first, last = changed[0], changed[-1]
    replacement = "".join(out[first : last + 1])
    return "".join(out), YamlDiff(fromLine=first + 1, toLine=last + 1, replacement=replacement)


def _canonicalize_api_actions(lines: list[str]) -> list[str]:
    """Respell the api block key and item discriminators."""
    api_span = _locate_singleton_block(lines, "api")
    if api_span is None:
        return lines
    listing = api_actions.locate_actions_list(lines, api_span)
    if listing is None:
        return lines
    actions_start, actions_end, item_indent, matched_key = listing
    # The block-key sub no-ops when already canonical; the item sub still
    # fixes legacy ``- service:`` discriminators under a canonical header.
    return api_actions.canonicalize_block(
        lines, actions_start, actions_end, item_indent, matched_key
    )


def _canonicalize_action_nodes(lines: list[str]) -> list[str]:
    """Respell registry-action node ids and their legacy body field."""
    out = list(lines)
    in_scalar = _block_scalar_mask(lines)
    for rename in _ACTION_NODE_RENAMES:
        anchor_re = re.compile(
            rf"^(?P<lead>[ ]*(?:-\s*)?)"
            rf"(?P<id>{re.escape(rename.legacy_id)}|{re.escape(rename.canonical_id)}):"
            rf"(?P<rest>.*)$"
        )
        for idx, line in enumerate(lines):
            if in_scalar[idx]:
                continue
            content = line.rstrip("\n\r")
            match = anchor_re.match(content)
            if match is None:
                continue
            new_id = (
                rename.canonical_id if match.group("id") == rename.legacy_id else match.group("id")
            )
            rest = match.group("rest")
            if rest.lstrip().startswith("{"):
                # Flow-style body on the anchor line itself.
                new_rest = _respell_flow_field(rest, rename)
                rest = new_rest if new_rest is not None else rest
                out[idx] = match.group("lead") + new_id + ":" + rest + line[len(content) :]
                continue
            out[idx] = match.group("lead") + new_id + ":" + rest + line[len(content) :]
            _respell_body_field(lines, out, idx, len(match.group("lead")), rename, in_scalar)
    return out


def _block_scalar_mask(lines: list[str]) -> list[bool]:
    """Mark lines inside ``|`` / ``>`` block scalars — never respell those."""
    mask = [False] * len(lines)
    scalar_indent: int | None = None
    for idx, line in enumerate(lines):
        content = line.rstrip("\n\r")
        if not content.strip():
            if scalar_indent is not None:
                mask[idx] = True
            continue
        leading = len(content) - len(content.lstrip(" "))
        if scalar_indent is not None:
            if leading > scalar_indent:
                mask[idx] = True
                continue
            scalar_indent = None
        if _SCALAR_HEADER_RE.search(content):
            scalar_indent = leading
    return mask


_SCALAR_HEADER_RE = re.compile(r"[|>][+-]?\d*\s*$")


def _respell_flow_field(rest: str, rename: ActionNodeRename) -> str | None:
    """Respell the depth-1 legacy field key in a flow-style body, or ``None``.

    Only keys of the outer flow mapping count, so a same-named key inside a
    nested ``data: {...}`` payload is never rewritten and never mistaken for
    the canonical key already being present.
    """
    keys = _flow_depth1_keys(rest)
    names = {name for _start, _end, name in keys}
    if rename.canonical_field in names or rename.legacy_field not in names:
        return None
    for start, end, name in keys:
        if name == rename.legacy_field:
            return rest[:start] + rename.canonical_field + rest[end:]
    return None


def _flow_depth1_keys(rest: str) -> list[tuple[int, int, str]]:
    """Return ``(start, end, name)`` for each depth-1 flow-mapping key."""
    keys: list[tuple[int, int, str]] = []
    depth = 0
    expecting_key = False
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < len(rest) and rest[i] != quote:
                i += 1
        elif ch in "{[":
            depth += 1
            expecting_key = depth == 1
        elif ch in "}]":
            depth -= 1
        elif ch == "," and depth == 1:
            expecting_key = True
        elif depth == 1 and expecting_key and not ch.isspace():
            name = re.match(r"[A-Za-z_][\w.]*", rest[i:])
            if name is not None:
                end = i + name.end()
                if rest[end:].lstrip().startswith(":"):
                    keys.append((i, end, name.group(0)))
                i = end - 1
            expecting_key = False
        i += 1
    return keys


def _respell_body_field(
    lines: list[str],
    out: list[str],
    anchor: int,
    content_col: int,
    rename: ActionNodeRename,
    in_scalar: list[bool],
) -> None:
    """Rename the node's own legacy field key to canonical in *out*.

    The body's child indent is taken from its first line; only a key at
    exactly that column counts, so a same-named key nested deeper (an
    action list inside ``data:``) stays untouched. Skips the rename when
    the canonical key already exists at that column — respelling would
    emit a duplicate key, and upstream validation already rejects the pair.
    """
    child_indent: str | None = None
    body_end = anchor
    for idx in range(anchor + 1, len(lines)):
        content = lines[idx].rstrip("\n\r")
        if not content.strip():
            continue
        leading = len(content) - len(content.lstrip(" "))
        if leading <= content_col:
            break
        if child_indent is None:
            child_indent = content[:leading]
        body_end = idx
    if child_indent is None:
        return
    legacy_re = re.compile(rf"^{re.escape(child_indent)}{re.escape(rename.legacy_field)}(\s*:)")
    canonical_re = re.compile(rf"^{re.escape(child_indent)}{re.escape(rename.canonical_field)}\s*:")
    body = [idx for idx in range(anchor + 1, body_end + 1) if not in_scalar[idx]]
    if any(canonical_re.match(lines[idx].rstrip("\n\r")) for idx in body):
        return
    for idx in body:
        stripped = lines[idx].rstrip("\n\r")
        new = legacy_re.sub(rf"{child_indent}{rename.canonical_field}\1", stripped, count=1)
        if new != stripped:
            out[idx] = new + lines[idx][len(stripped) :]
            return
