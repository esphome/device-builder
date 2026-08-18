"""
Whole-file migration of deprecated options to their replacements.

Each migration is a pure, line-for-line ``lines -> lines`` rule in
``_RULES``; ``render_migrations`` folds them into one splice, so a
single click brings a config fully up to date. Line edits (never
parse-and-re-emit) keep comments and formatting intact and let the
command run against a mid-edit draft that ``automations/parse`` would
reject. Bespoke rules cover the anchors the generic machinery can't
express; plain ``cv.rename_key`` pairs arrive data-driven from the
sync-generated ``migration_rules.index.json``. Exposed as
``editor/migrate_config``; ``has_pending_migrations`` feeds the
per-device dashboard flag at scanner load time.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

from ..definitions import MigrationRule, load_migration_rules_index
from ..models.automations import YamlDiff
from .yaml import api_actions
from .yaml.scalar import parse_config_boolean
from .yaml.scan import (
    block_end_index,
    child_block_end,
    is_list_item_line,
    leading_ws,
    top_level_key_index,
    top_list_item_starts,
)
from .yaml.writing_layout import (
    _build_diff_for_append,
    _locate_singleton_block,
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

_ETHERNET_CLK_MODE_KEY = "clk_mode"


def render_migrations(yaml_text: str) -> tuple[str, YamlDiff] | None:
    """
    Apply every known migration to *yaml_text*.

    Returns ``(new_text, diff)`` with one contiguous splice covering the
    changed span, or ``None`` when nothing needed migrating.
    """
    out = yaml_text.splitlines(keepends=True)
    for rule, _tokens in _RULES:
        out = rule(out)
    new_text = "".join(out)
    if new_text == yaml_text:
        return None
    return new_text, _build_diff_for_append(yaml_text, new_text)


def has_pending_migrations(yaml_text: str) -> bool:
    """Report whether ``render_migrations`` would change *yaml_text*."""
    # Fleet-scan hot path: the token scan is ~300x cheaper than the fold.
    if _legacy_token_re(load_migration_rules_index()).search(yaml_text) is None:
        return False
    return render_migrations(yaml_text) is not None


@cache
def _legacy_token_re(rules: tuple[MigrationRule, ...]) -> re.Pattern[str]:
    """Alternation matching every rule's legacy spelling in key position."""
    # ``\s*:`` mirrors the loosest matcher (split-on-colon tolerates padding
    # before the colon) so prose mentions don't force the full fold.
    tokens = _BESPOKE_LEGACY_TOKENS.union(rule.old for rule in rules)
    return re.compile("|".join(re.escape(token) + r"\s*:" for token in sorted(tokens)))


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
            hit = _respell_body_field(
                lines,
                idx,
                len(match.group("lead")),
                rename.legacy_field,
                rename.canonical_field,
                in_scalar,
            )
            if hit is not None:
                out[hit[0]] = hit[1]
    return out


# Shapes the generic rules deliberately don't reach — check when the
# first real pair lands: flow-style list items, and the legacy
# bare-mapping ``ota:`` / ``time:`` form (implicit platform).
def _apply_generated_renames(lines: list[str]) -> list[str]:
    """Apply the sync-discovered rename rules from the generated artifact."""
    key_rules, block_groups, item_groups, fold_groups = _group_generated_rules(
        load_migration_rules_index()
    )
    out = list(lines)
    headers = top_level_key_index(out)
    # Key respells first, so a same-release field rename inside the
    # renamed block still converges in this single fold pass.
    for rule in key_rules:
        _respell_top_level_key(out, headers, rule.old, rule.new)

    in_scalar: list[bool] | None = None

    def mask() -> list[bool]:
        # One pass serves every block: the respells below preserve line
        # count, indentation, and everything after the key.
        nonlocal in_scalar
        if in_scalar is None:
            in_scalar = _block_scalar_mask(out)
        return in_scalar

    for component, rules in block_groups.items():
        if (header := headers.get(component)) is not None:
            _apply_component_block_fields(out, header, rules, mask())
    for domain, by_platform in item_groups.items():
        if (header := headers.get(domain)) is not None:
            _apply_platform_item_fields(out, header, by_platform, mask())
    # Folds last — they delete lines, so each re-anchors on fresh indexes.
    for domain, platforms in fold_groups.items():
        if (header := top_level_key_index(out).get(domain)) is not None:
            out = _fold_channel_colors_items(out, header, platforms)
    return out


def _group_generated_rules(
    rules: tuple[MigrationRule, ...],
) -> tuple[
    list[MigrationRule],
    dict[str, list[MigrationRule]],
    dict[str, dict[str, list[MigrationRule]]],
    dict[str, set[str]],
]:
    """Split *rules* into key respells, block groups, item groups, and fold platform sets."""
    key_rules: list[MigrationRule] = []
    block_groups: dict[str, list[MigrationRule]] = {}
    item_groups: dict[str, dict[str, list[MigrationRule]]] = {}
    fold_groups: dict[str, set[str]] = {}
    for rule in rules:
        # No fallthrough: a kind this build doesn't know is dropped here,
        # never misapplied as some other kind's rename.
        if rule.kind == "component_key":
            key_rules.append(rule)
        elif rule.kind == "component_block_field":
            block_groups.setdefault(rule.component, []).append(rule)
        elif rule.kind == "platform_item_field":
            item_groups.setdefault(rule.domain, {}).setdefault(rule.platform, []).append(rule)
        elif rule.kind == "platform_channel_colors":
            fold_groups.setdefault(rule.domain, set()).add(rule.platform)
    return key_rules, block_groups, item_groups, fold_groups


def _apply_component_block_fields(
    lines: list[str],
    header: int,
    rules: list[MigrationRule],
    in_scalar: list[bool],
) -> None:
    """Rename child keys of the top-level block at *header* in place, mapping or list form."""
    end = block_end_index(lines, header)
    # The block's first content line decides its form; a deeper dash
    # inside a mapping body is a nested list, not a multi_conf item.
    list_form = False
    for idx in range(header + 1, min(end, len(lines))):
        # A scalar open before the column-0 header ends at it, so a masked
        # line can't precede the block's first content line.
        if in_scalar[idx]:  # pragma: no cover
            continue
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("#"):
            continue
        list_form = is_list_item_line(stripped)
        break
    if not list_form:
        for rule in rules:
            hit = _respell_body_field(lines, header, 0, rule.old, rule.new, in_scalar)
            if hit is not None:
                lines[hit[0]] = hit[1]
        return
    for item_start in top_list_item_starts(lines, header, end):
        keys = _item_child_keys(lines, item_start, end, in_scalar)
        for rule in rules:
            _respell_item_key(lines, keys, rule.old, rule.new)


def _apply_platform_item_fields(
    lines: list[str],
    header: int,
    rules_by_platform: dict[str, list[MigrationRule]],
    in_scalar: list[bool],
) -> None:
    """Rename child keys of the ``- platform:`` items in the domain block at *header* in place."""
    end = block_end_index(lines, header)
    for item_start in top_list_item_starts(lines, header, end):
        keys = _item_child_keys(lines, item_start, end, in_scalar)
        platform = next(
            (_entry_value(lines[idx], col) for idx, col, key in keys if key == "platform"),
            None,
        )
        if platform is None:
            continue
        for rule in rules_by_platform.get(platform, ()):
            _respell_item_key(lines, keys, rule.old, rule.new)


def _respell_item_key(
    lines: list[str],
    keys: list[tuple[int, int, str]],
    old: str,
    new: str,
) -> None:
    """Respell an item's *old* depth-1 key in *lines* and *keys*; no-op on collision or absence."""
    if any(key == new for _idx, _col, key in keys):
        return
    slot = next((i for i, (_idx, _col, key) in enumerate(keys) if key == old), None)
    if slot is None:
        return
    idx, col, _key = keys[slot]
    content = lines[idx].rstrip("\n\r")
    eol = lines[idx][len(content) :]
    lines[idx] = content[:col] + new + content[col + len(old) :] + eol
    keys[slot] = (idx, col, new)


#: Upstream ``light.RGB_ORDERS`` — the closed value set of the deprecated
#: ``rgb_order`` key; anything else must keep failing validation loudly.
_RGB_ORDERS = frozenset({"RGB", "RBG", "GRB", "GBR", "BGR", "BRG"})


def _fold_channel_colors_items(
    lines: list[str],
    header: int,
    platforms: set[str],
) -> list[str]:
    """
    Fold ``rgb_order`` / ``is_rgbw`` / ``is_wrgb`` into ``channel_colors``.

    Applied to the *platforms* items of the domain block at *header*
    (the ``platform_channel_colors`` rule kind, esphome/esphome#18474).
    ``is_wrgb`` prepends ``W`` to the order, ``is_rgbw`` appends it. An
    undecodable item is left alone; an existing ``channel_colors``
    entry is replaced wholesale.
    """
    end = block_end_index(lines, header)
    in_scalar = _block_scalar_mask(lines)
    out = lines
    # Last item first, so earlier items' line indexes stay valid in *out*.
    for item_start in reversed(top_list_item_starts(lines, header, end)):
        keys = _item_child_keys(lines, item_start, end, in_scalar)
        entries = {key: (idx, col) for idx, col, key in keys}
        platform = entries.get("platform")
        if platform is None or _entry_value(lines[platform[0]], platform[1]) not in platforms:
            continue
        folded = _fold_channel_colors(lines, entries)
        if folded is None:
            continue
        (anchor_idx, anchor_col), value, delete = folded
        # Deleting the dash line would orphan the rest of the item.
        if any(idx == item_start for idx, _col in delete):
            continue
        content = lines[anchor_idx].rstrip("\n\r")
        eol = lines[anchor_idx][len(content) :] or "\n"
        comment = ""
        comment_match = _TRAILING_COMMENT_RE.search(content[anchor_col:].split(":", 1)[1])
        if comment_match is not None:
            comment = "  " + comment_match.group(1)
        replacement = f"{content[:anchor_col]}channel_colors: {value}{comment}{eol}"
        if out is lines:
            out = list(lines)
        splices = [(anchor_idx, [replacement])] + [(idx, []) for idx, _col in delete]
        for idx, rep in sorted(splices, key=lambda t: -t[0]):
            out[idx : idx + 1] = rep
    return out


def _fold_channel_colors(
    lines: list[str],
    entries: dict[str, tuple[int, int]],
) -> tuple[tuple[int, int], str, list[tuple[int, int]]] | None:
    """
    ``(anchor entry, folded value, entries to delete)`` for one strip item.

    ``None`` when there is nothing to fold or a value falls outside the
    closed tables (the config must keep failing validation loudly).
    """
    order = entries.get("rgb_order")
    if order is None:
        return None
    flags: dict[str, bool] = {}
    delete: list[tuple[int, int]] = []
    for key in ("is_rgbw", "is_wrgb"):
        entry = entries.get(key)
        if entry is None:
            flags[key] = False
            continue
        decoded = parse_config_boolean(_entry_value(lines[entry[0]], entry[1]))
        if decoded is None:
            return None
        flags[key] = decoded
        delete.append(entry)
    if flags["is_rgbw"] and flags["is_wrgb"]:
        return None
    value = _entry_value(lines[order[0]], order[1]).upper()
    if value not in _RGB_ORDERS:
        return None
    if flags["is_wrgb"]:
        value = f"W{value}"
    elif flags["is_rgbw"]:
        value = f"{value}W"
    existing = entries.get("channel_colors")
    if existing is not None:
        delete.append(existing)
    return order, value, delete


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
    header = child_indent + _ETHERNET_CLK_MODE_KEY + ":"
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
        return idx, child_block_end(lines, idx, min(end, len(lines)), child_indent)
    return None


def _respell_top_level_key(
    lines: list[str],
    headers: dict[str, int],
    legacy: str,
    canonical: str,
) -> None:
    """
    Rename the column-0 ``legacy:`` header in place, keeping *headers* current.

    No-op when a ``canonical:`` block already exists.
    """
    idx = headers.get(legacy)
    if idx is None or canonical in headers:
        return
    lines[idx] = canonical + lines[idx][len(legacy) :]
    headers[canonical] = headers.pop(legacy)


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
    legacy_field: str,
    canonical_field: str,
    in_scalar: list[bool],
) -> tuple[int, str] | None:
    """
    Return ``(line index, respelled line)`` for the body's legacy field.

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
        if key == canonical_field:
            return None
        if key == legacy_field and hit is None:
            new = child_indent + canonical_field + content[len(child_indent) + len(key) :]
            hit = (idx, new + lines[idx][len(content) :])
    return hit


def _item_child_keys(
    lines: list[str],
    item_start: int,
    end: int,
    in_scalar: list[bool],
) -> list[tuple[int, int, str]]:
    """
    ``(line, column, key)`` for a list item's depth-1 mapping keys.

    The dash-line key comes first; following lines count only at its
    column (or, for a bare dash, the first child indent seen).
    """
    keys: list[tuple[int, int, str]] = []
    content = lines[item_start].rstrip("\n\r")
    dash_col = len(leading_ws(content))
    rest = content[dash_col + 1 :]
    child_col: int | None = None
    stripped = rest.lstrip(" ")
    if stripped and not stripped.startswith(("#", "{", "[")) and ":" in stripped:
        child_col = dash_col + 1 + (len(rest) - len(stripped))
        keys.append((item_start, child_col, stripped.split(":", 1)[0].rstrip()))
    for idx in range(item_start + 1, min(end, len(lines))):
        if in_scalar[idx]:
            continue
        line = lines[idx].rstrip("\n\r")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(leading_ws(line))
        if indent <= dash_col:
            break
        child_col = child_col if child_col is not None else indent
        if indent != child_col or ":" not in stripped:
            continue
        keys.append((idx, indent, stripped.split(":", 1)[0].rstrip()))
    return keys


def _entry_value(line: str, col: int) -> str:
    """Return the ``key: value`` entry's scalar at *col*, comment and quotes stripped."""
    value = line.rstrip("\n\r")[col:].split(":", 1)[1].strip()
    comment = _TRAILING_COMMENT_RE.search(value)
    if comment is not None:
        value = value[: comment.start()].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


# Each bespoke rule is paired with the substrings it needs present before it
# can fire, so a rule can't join the fold without feeding the prefilter; the
# generated leg's tokens join from the rules index at predicate time.
_RULES: tuple[tuple[Callable[[list[str]], list[str]], frozenset[str]], ...] = (
    (
        _canonicalize_api_actions,
        frozenset((*api_actions.BLOCK_KEYS[1:], *api_actions.ITEM_KEYS[1:])),
    ),
    (
        _canonicalize_action_nodes,
        frozenset(t for r in _ACTION_NODE_RENAMES for t in (r.legacy_id, r.legacy_field)),
    ),
    (_migrate_ethernet_clk, frozenset((_ETHERNET_CLK_MODE_KEY,))),
    (_apply_generated_renames, frozenset()),
)
_BESPOKE_LEGACY_TOKENS = frozenset().union(*(tokens for _, tokens in _RULES))
