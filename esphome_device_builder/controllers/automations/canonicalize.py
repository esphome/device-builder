"""
Whole-file respell of legacy renamed-key spellings to canonical.

Each rename shape is a pure, line-for-line ``lines -> lines`` rule in
``_RULES``; ``render_canonicalize`` folds them. Line edits (never
parse-and-re-emit) keep comments and formatting intact and let the
command run against a mid-edit draft that ``automations/parse`` would
reject. Lives beside the ``api_actions`` / ``writing_layout`` locators
it reuses; exposed as an ``editor/`` command. Detection is mirrored in
device-builder-frontend ``src/util/yaml-automations-legacy.ts`` — new
cases land in both suites.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...helpers.yaml.scan import leading_ws
from ...models.automations import YamlDiff
from . import api_actions
from .writing_layout import _build_diff_for_append, _locate_singleton_block


@dataclass(frozen=True)
class ActionNodeRename:
    """A registry action's legacy/canonical node ids and body field."""

    legacy_id: str
    canonical_id: str
    legacy_field: str
    canonical_field: str

    @property
    def anchor_re(self) -> re.Pattern[str]:
        return re.compile(
            rf"^(?P<lead>[ ]*(?:-\s*)?)"
            rf"(?P<id>{re.escape(self.legacy_id)}|{re.escape(self.canonical_id)}):"
            rf"(?P<rest>.*)$"
        )


_ACTION_NODE_RENAMES = (
    ActionNodeRename("homeassistant.service", "homeassistant.action", "service", "action"),
)

_ANCHOR_RES = tuple((rename, rename.anchor_re) for rename in _ACTION_NODE_RENAMES)

_SCALAR_HEADER_RE = re.compile(r"[|>][+-]?\d*\s*$")


def render_canonicalize(yaml_text: str) -> tuple[str, YamlDiff] | None:
    """
    Respell every legacy spelling in *yaml_text* to canonical.

    Returns ``(new_text, diff)`` with one contiguous splice covering the
    changed span, or ``None`` when nothing is legacy.
    """
    out = yaml_text.splitlines(keepends=True)
    for rule in _RULES:
        out = rule(out)
    new_text = "".join(out)
    if new_text == yaml_text:
        return None
    return new_text, _build_diff_for_append(yaml_text, new_text)


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
    for rename, anchor_re in _ANCHOR_RES:
        for idx, line in enumerate(lines):
            if in_scalar[idx]:
                continue
            content = line.rstrip("\n\r")
            match = anchor_re.match(content)
            if match is None:
                continue
            rest = match.group("rest")
            flow = rest.lstrip().startswith("{")
            if flow:
                rest = _respell_flow_field(rest, rename)
            out[idx] = match.group("lead") + rename.canonical_id + ":" + rest + line[len(content) :]
            if not flow:
                hit = _respell_body_field(lines, idx, len(match.group("lead")), rename, in_scalar)
                if hit is not None:
                    out[hit[0]] = hit[1]
    return out


_RULES = (_canonicalize_api_actions, _canonicalize_action_nodes)


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
        leading = len(leading_ws(content))
        if scalar_indent is not None:
            if leading > scalar_indent:
                mask[idx] = True
                continue
            scalar_indent = None
        if _SCALAR_HEADER_RE.search(content):
            scalar_indent = leading
    return mask


def _respell_flow_field(rest: str, rename: ActionNodeRename) -> str:
    """Respell the depth-1 legacy field key in a flow-style body.

    Only keys of the outer flow mapping count, so a same-named key inside a
    nested ``data: {...}`` payload is never rewritten and never mistaken for
    the canonical key already being present. Unchanged when the canonical
    key is present or the legacy one is absent.
    """
    keys = _flow_depth1_keys(rest)
    if any(name == rename.canonical_field for _start, _end, name in keys):
        return rest
    legacy = next(((start, end) for start, end, name in keys if name == rename.legacy_field), None)
    if legacy is None:
        return rest
    start, end = legacy
    return rest[:start] + rename.canonical_field + rest[end:]


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
    anchor: int,
    content_col: int,
    rename: ActionNodeRename,
    in_scalar: list[bool],
) -> tuple[int, str] | None:
    """Return ``(line index, respelled line)`` for the node's legacy field.

    The body's child indent is taken from its first line; only a key at
    exactly that column counts, so a same-named key nested deeper (an
    action list inside ``data:``) stays untouched. ``None`` when the field
    is absent — or when the canonical key already exists at that column,
    since respelling would emit a duplicate key and upstream validation
    already rejects the pair. Comment lines neither bound the body nor
    pick its indent.
    """
    child_indent: str | None = None
    hit: tuple[int, str] | None = None
    for idx in range(anchor + 1, len(lines)):
        if in_scalar[idx]:
            continue
        content = lines[idx].rstrip("\n\r")
        stripped = content.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = leading_ws(content)
        if len(indent) <= content_col:
            break
        child_indent = child_indent if child_indent is not None else indent
        if indent != child_indent:
            continue
        key = stripped.split(":", 1)[0].rstrip()
        if key == rename.canonical_field:
            return None
        if key == rename.legacy_field and hit is None:
            new = child_indent + rename.canonical_field + content[len(child_indent) + len(key) :]
            hit = (idx, new + lines[idx][len(content) :])
    return hit
