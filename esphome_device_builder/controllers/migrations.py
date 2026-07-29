"""
Whole-file migration of deprecated options to their replacements.

Each migration is a pure, line-for-line ``lines -> lines`` rule in
``_RULES``; ``render_migrations`` folds them into one splice, so a
single click brings a config fully up to date. Line edits (never
parse-and-re-emit) keep comments and formatting intact and let the
command run against a mid-edit draft that ``automations/parse`` would
reject. Exposed as ``editor/migrate_config``; detection is mirrored in
device-builder-frontend ``src/util/config-migrations.ts`` — new rules
land on both sides.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..helpers.yaml.scan import leading_ws
from ..models.automations import YamlDiff
from .automations import api_actions
from .automations.writing_layout import (
    _build_diff_for_append,
    _locate_singleton_block,
    _trim_trailing_gap,
)


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

_SCALAR_HEADER_RE = re.compile(r"[|>][0-9+-]*\s*$")


def render_migrations(yaml_text: str) -> tuple[str, YamlDiff] | None:
    """
    Apply every known migration to *yaml_text*.

    Returns ``(new_text, diff)`` with one contiguous splice covering the
    changed span, or ``None`` when nothing needed migrating.
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
    for rename, anchor_re in _ANCHOR_RES:
        out = _apply_action_node_rename(out, rename, anchor_re)
    return out


def _apply_action_node_rename(
    lines: list[str],
    rename: ActionNodeRename,
    anchor_re: re.Pattern[str],
) -> list[str]:
    """Apply one rename table entry across the whole file."""
    out = list(lines)
    in_scalar = _block_scalar_mask(lines)
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


#: Upstream's closed ``CLK_MODES_DEPRECATED`` table — anything else is an
#: invalid config that must keep failing validation loudly.
_CLK_MODES = {
    "GPIO0_IN": ("GPIO0", "CLK_EXT_IN"),
    "GPIO0_OUT": ("GPIO0", "CLK_OUT"),
    "GPIO16_OUT": ("GPIO16", "CLK_OUT"),
    "GPIO17_OUT": ("GPIO17", "CLK_OUT"),
}

_TRAILING_COMMENT_RE = re.compile(r"\s(#.*)$")


def _migrate_ethernet_clk(lines: list[str]) -> list[str]:
    """
    Migrate flat ``clk_mode`` to the nested ``clk`` block.

    Pin and direction come from upstream's closed mode table; a value
    outside it is left alone. An existing ``clk:`` block is replaced
    wholesale; a trailing comment on the old line moves to ``clk:``.
    """
    span = _locate_singleton_block(lines, "ethernet")
    if span is None:
        return lines
    start, end, child_indent = span
    header = child_indent + "clk_mode:"
    for idx in range(start + 1, min(end, len(lines))):
        content = lines[idx].rstrip("\n\r")
        if not content.startswith(header):
            continue
        value = content[len(header) :].strip()
        comment = ""
        comment_match = _TRAILING_COMMENT_RE.search(value)
        if comment_match is not None:
            comment = "  " + comment_match.group(1)
            value = value[: comment_match.start()].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        decoded = _CLK_MODES.get(value.strip().upper().replace(" ", "_"))
        if decoded is None:
            return lines
        pin, mode = decoded
        eol = lines[idx][len(content) :] or "\n"
        replacement = [
            f"{child_indent}clk:{comment}{eol}",
            f"{child_indent}  pin: {pin}{eol}",
            f"{child_indent}  mode: {mode}{eol}",
        ]
        out = list(lines)
        splices = [(idx, idx + 1, replacement)]
        clk_span = _child_block_span(lines, start, end, child_indent, "clk")
        if clk_span is not None and clk_span[0] != idx:
            splices.append((clk_span[0], clk_span[1], []))
        for splice_start, splice_end, rep in sorted(splices, key=lambda t: -t[0]):
            out[splice_start:splice_end] = rep
        return out
    return lines


def _child_block_span(
    lines: list[str],
    start: int,
    end: int,
    child_indent: str,
    key: str,
) -> tuple[int, int] | None:
    """Line span of the ``key:`` child (header plus deeper lines), or ``None``."""
    header = child_indent + key + ":"
    for idx in range(start + 1, min(end, len(lines))):
        content = lines[idx].rstrip("\n\r")
        if content != header and not content.startswith(header + " "):
            continue
        stop = idx + 1
        while stop < min(end, len(lines)):
            deeper = lines[stop].rstrip("\n\r")
            if deeper.strip() and len(leading_ws(deeper)) <= len(child_indent):
                break
            stop += 1
        return idx, _trim_trailing_gap(lines, idx, stop)
    return None


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
    """
    Respell the depth-1 legacy field key in a flow-style body.

    Only keys of the outer flow mapping count. Unchanged when the
    canonical key is present or the legacy one is absent.
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
    """
    Return ``(line index, respelled line)`` for the node's legacy field.

    Only a key at exactly the body's child-indent column counts, so a
    same-named key nested deeper stays untouched. ``None`` when the
    field is absent or the canonical key already exists at that column.
    Comment lines neither bound the body nor pick its indent.
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


_RULES = (_canonicalize_api_actions, _canonicalize_action_nodes, _migrate_ethernet_clk)
