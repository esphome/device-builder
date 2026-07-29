"""
Whole-file respell of legacy renamed-key spellings to canonical.

The on-demand counterpart to the write-path respells: covers the api
``services:`` block and item discriminators plus the homeassistant
action's legacy node id and ``service:`` body field, line for line, so
comments and formatting survive.
"""

from __future__ import annotations

import re

from ...models.automations import YamlDiff
from . import api_actions
from .writing_layout import _locate_singleton_block

#: Anchor for a homeassistant action node — a list item or mapping key
#: whose id is either registered spelling. Comment lines can't match
#: (the id must open the line's content).
_HA_ANCHOR_RE = re.compile(
    r"^(?P<lead>[ ]*(?:-\s*)?)(?P<id>homeassistant\.(?:service|action)):(?P<rest>.*)$"
)

_HA_CANONICAL_ID = "homeassistant.action"
_FLOW_SERVICE_RE = re.compile(r"([{,]\s*)service(\s*:)")
_FLOW_ACTION_RE = re.compile(r"[{,]\s*action\s*:")


def render_canonicalize(yaml_text: str) -> tuple[str, YamlDiff] | None:
    """
    Respell every legacy spelling in *yaml_text* to canonical.

    Returns ``(new_text, diff)`` with one contiguous splice covering the
    first through last changed line, or ``None`` when nothing is legacy.
    """
    lines = yaml_text.splitlines(keepends=True)
    out = _canonicalize_api(list(lines))
    out = _canonicalize_homeassistant(out)
    if out == lines:
        return None
    changed = [idx for idx in range(len(lines)) if lines[idx] != out[idx]]
    first, last = changed[0], changed[-1]
    replacement = "".join(out[first : last + 1])
    return "".join(out), YamlDiff(fromLine=first + 1, toLine=last + 1, replacement=replacement)


def _canonicalize_api(lines: list[str]) -> list[str]:
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


def _canonicalize_homeassistant(lines: list[str]) -> list[str]:
    """Respell homeassistant action node ids and their legacy body field."""
    out = list(lines)
    for idx, line in enumerate(lines):
        match = _HA_ANCHOR_RE.match(line.rstrip("\n\r"))
        if match is None:
            continue
        if match.group("id") != _HA_CANONICAL_ID:
            out[idx] = out[idx].replace(match.group("id"), _HA_CANONICAL_ID, 1)
        rest = match.group("rest")
        if "{" in rest:
            # Flow-style body on the anchor line itself.
            if _FLOW_ACTION_RE.search(rest) is None:
                out[idx] = _FLOW_SERVICE_RE.sub(r"\1action\2", out[idx], count=1)
            continue
        _respell_body_field(lines, out, idx, content_col=len(match.group("lead")))
    return out


def _respell_body_field(
    lines: list[str],
    out: list[str],
    anchor: int,
    content_col: int,
) -> None:
    """Rename the node's own ``service:`` key to ``action:`` in *out*.

    The body's child indent is taken from its first line; only a key at
    exactly that column counts, so a same-named key nested deeper (an
    action list inside ``data:``) stays untouched. Skips the rename when
    ``action:`` already exists at that column — respelling would emit a
    duplicate key, and upstream validation already rejects the pair.
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
    service_re = re.compile(rf"^{re.escape(child_indent)}service(\s*:)")
    action_re = re.compile(rf"^{re.escape(child_indent)}action\s*:")
    body = range(anchor + 1, body_end + 1)
    if any(action_re.match(lines[idx].rstrip("\n\r")) for idx in body):
        return
    for idx in body:
        new = service_re.sub(rf"{child_indent}action\1", lines[idx].rstrip("\n\r"), count=1)
        if new != lines[idx].rstrip("\n\r"):
            out[idx] = new + lines[idx][len(lines[idx].rstrip("\n\r")) :]
            return
