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
    for rename in _ACTION_NODE_RENAMES:
        anchor_re = re.compile(
            rf"^(?P<lead>[ ]*(?:-\s*)?)"
            rf"(?P<id>{re.escape(rename.legacy_id)}|{re.escape(rename.canonical_id)}):"
            rf"(?P<rest>.*)$"
        )
        flow_field_re = re.compile(rf"([{{,]\s*){re.escape(rename.legacy_field)}(\s*:)")
        flow_canonical_re = re.compile(rf"[{{,]\s*{re.escape(rename.canonical_field)}\s*:")
        for idx, line in enumerate(lines):
            match = anchor_re.match(line.rstrip("\n\r"))
            if match is None:
                continue
            if match.group("id") == rename.legacy_id:
                out[idx] = out[idx].replace(rename.legacy_id, rename.canonical_id, 1)
            rest = match.group("rest")
            if "{" in rest:
                # Flow-style body on the anchor line itself.
                if flow_canonical_re.search(rest) is None:
                    out[idx] = flow_field_re.sub(
                        rf"\1{rename.canonical_field}\2", out[idx], count=1
                    )
                continue
            _respell_body_field(lines, out, idx, len(match.group("lead")), rename)
    return out


def _respell_body_field(
    lines: list[str],
    out: list[str],
    anchor: int,
    content_col: int,
    rename: ActionNodeRename,
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
    body = range(anchor + 1, body_end + 1)
    if any(canonical_re.match(lines[idx].rstrip("\n\r")) for idx in body):
        return
    for idx in body:
        stripped = lines[idx].rstrip("\n\r")
        new = legacy_re.sub(rf"{child_indent}{rename.canonical_field}\1", stripped, count=1)
        if new != stripped:
            out[idx] = new + lines[idx][len(stripped) :]
            return
