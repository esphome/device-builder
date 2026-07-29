"""
``api.actions:`` splice helpers used by :mod:`writing`.

The ``api:`` block hosts a list of named callables under ``actions:``
— structurally near-identical to ``script:``, but nested two levels
deep rather than at the top level. The lookup, item-locator, and
re-indent helpers live here so :mod:`writing` stays close to the
800-line file-size cap; the dispatch surface (the
``_upsert_api_action`` / ``_delete_api_action`` branches of
:func:`writing.render_upsert` / :func:`writing.render_delete`)
stays in :mod:`writing` alongside every other location type for
grep-ability.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import NamedTuple

from ...models.automations import YamlDiff

#: Canonical key spellings used when no catalog data is supplied; the
#: catalog-driven tuples always list today's canonical spelling first.
CANONICAL_BLOCK_KEYS = ("actions",)
CANONICAL_ITEM_KEYS = ("action",)


class ActionsSpan(NamedTuple):
    """The located api action list: line span, item indent, matched key."""

    start: int
    end: int
    item_indent: str
    key: str
    # The block key carries a flow-style value (``actions: []``) the
    # line-based writer can't splice into; callers must refuse.
    inline: bool


def locate_actions_list(
    lines: list[str],
    api_span: tuple[int, int, str],
    keys: tuple[str, ...],
) -> ActionsSpan | None:
    """Locate the api action list under *api_span*, matching any spelling in *keys*.

    First spelling wins when several appear; ``end`` is one past the
    last item line.
    """
    api_end, child_indent = api_span[1], api_span[2]
    found = _find_block_key_line(lines, api_span, keys)
    if found is None:
        return None
    actions_start, matched_key = found
    rest = lines[actions_start].rstrip("\n\r")[len(f"{child_indent}{matched_key}:") :].strip()
    if rest and not rest.startswith("#"):
        return ActionsSpan(actions_start, actions_start, "", matched_key, inline=True)
    actions_end = api_end
    for idx in range(actions_start + 1, api_end):
        content = lines[idx].rstrip("\n\r")
        if not content:
            continue
        leading = len(content) - len(content.lstrip(" "))
        if leading <= len(child_indent):
            actions_end = idx
            break
    item_indent: str | None = None
    for idx in range(actions_start + 1, actions_end):
        raw = lines[idx].rstrip("\n\r")
        stripped = raw.lstrip(" ")
        if stripped.startswith("- "):
            item_indent = raw[: len(raw) - len(stripped)]
            break
    if item_indent is None:
        # Empty list — assume the canonical two-space nesting under
        # ``actions:`` so the first item still indents predictably.
        item_indent = child_indent + "  "
    return ActionsSpan(actions_start, actions_end, item_indent, matched_key, inline=False)


def canonicalize_block(
    lines: list[str],
    span: ActionsSpan,
    block_keys: tuple[str, ...],
    item_keys: tuple[str, ...],
) -> list[str]:
    """Respell the block key and item discriminators to canonical, line for line.

    Only the key tokens change; every other byte (indent, values,
    comments, sibling lines) passes through untouched.
    """
    out = list(lines)
    header = out[span.start]
    out[span.start] = re.sub(
        rf"^(\s*){re.escape(span.key)}:", rf"\g<1>{block_keys[0]}:", header, count=1
    )
    legacy_alt = "|".join(re.escape(key) for key in item_keys[1:])
    if legacy_alt:
        # Discriminators live on the dash line or at the item's child
        # indent — never deeper, so a same-named key inside an action
        # body (``homeassistant.service`` params) stays untouched.
        item_re = re.compile(rf"^({re.escape(span.item_indent)}(?:-\s*|  ))(?:{legacy_alt}):")
        for idx in range(span.start + 1, span.end):
            out[idx] = item_re.sub(rf"\g<1>{item_keys[0]}:", out[idx], count=1)
    return out


def find_item(
    lines: list[str],
    actions_start: int,
    actions_end: int,
    item_indent: str,
    action_name: str,
    keys: tuple[str, ...],
) -> tuple[int, int] | None:
    """Locate the line range of the list item whose discriminator matches."""
    item_starts: list[int] = []
    for idx in range(actions_start + 1, actions_end):
        raw = lines[idx].rstrip("\n\r")
        if not raw.startswith(item_indent + "- "):
            continue
        item_starts.append(idx)
    for run, start in enumerate(item_starts):
        end = item_starts[run + 1] if run + 1 < len(item_starts) else actions_end
        if _discriminator(lines, start, end, item_indent, keys) == action_name:
            return start, end
    return None


def count_siblings(
    lines: list[str],
    actions_start: int,
    actions_end: int,
    item_indent: str,
    matched: tuple[int, int],
) -> int:
    """Count list items at *item_indent* that aren't the matched span."""
    item_start, item_end = matched
    siblings = 0
    for idx in range(actions_start + 1, actions_end):
        raw = lines[idx].rstrip("\n\r")
        if not raw.startswith(item_indent + "- "):
            continue
        if item_start <= idx < item_end:
            continue
        siblings += 1
    return siblings


def indent_for_list(rendered_item: str, item_indent: str) -> str:
    """Re-indent a ``dump([item])`` block to nest under ``api.actions:``.

    The shared ruamel emitter writes top-level list items at column
    two (``  - key: value``) because ``offset=2`` indents each dash
    two columns inside its parent sequence. ``api.actions:`` items
    live two levels deeper than that, so add the *item_indent* - 2
    delta to every non-empty line. Blank lines inside ``|`` block
    scalars (e.g. a multi-paragraph ``lambda:`` body) are left
    untouched — padding them with spaces would change the scalar's
    content, since YAML treats whitespace-only lines and fully
    empty lines differently inside a literal block.
    """
    pad = " " * (len(item_indent) - 2)
    out_lines: list[str] = []
    for line in rendered_item.splitlines():
        if not line:
            out_lines.append("")
            continue
        out_lines.append(pad + line)
    if not out_lines or out_lines[-1] != "":
        out_lines.append("")
    return "\n".join(out_lines)


def render_replacement(
    lines: list[str],
    item_start: int,
    item_end: int,
    rendered_text: str,
) -> tuple[str, YamlDiff]:
    """Splice *rendered_text* over the lines spanning an existing item."""
    new_lines = [*lines[:item_start], rendered_text, *lines[item_end:]]
    return "".join(new_lines), YamlDiff(
        fromLine=item_start + 1,
        toLine=item_end,
        replacement=rendered_text,
    )


def render_create_block(yaml_text: str, rendered: str, block_key: str) -> tuple[str, str]:
    """Return ``(new_yaml, block_text)`` for a fresh ``api:`` block at EOF."""
    item_indent = "    "
    item_text = indent_for_list(rendered, item_indent)
    # ``item_text`` always ends with ``\n``, so the block does too and
    # the final concatenation lands a trailing newline on EOF.
    block = f"api:\n  {block_key}:\n" + item_text
    base = yaml_text.rstrip()
    separator = "\n\n" if base else ""
    return f"{base}{separator}{block}", block


def render_insert_actions_key(
    lines: list[str],
    api_span: tuple[int, int, str],
    rendered: str,
    block_key: str,
) -> tuple[str, YamlDiff]:
    """Insert the canonical block key with one item under an existing ``api:`` block."""
    api_start, api_end, child_indent = api_span
    item_indent = child_indent + "  "
    item_text = indent_for_list(rendered, item_indent)
    block = f"{child_indent}{block_key}:\n{item_text}"
    insert_at = api_end
    while insert_at > api_start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new_lines = [*lines[:insert_at], block, *lines[insert_at:]]
    new_text = "".join(new_lines)
    return new_text, YamlDiff(
        fromLine=insert_at + 1,
        toLine=insert_at,
        replacement=block,
    )


def render_append(
    lines: list[str],
    actions_end: int,
    item_indent: str,
    rendered: str,
) -> tuple[str, YamlDiff]:
    """Append a new list item at the end of an existing ``api.actions:``."""
    item_text = indent_for_list(rendered, item_indent)
    insert_at = actions_end
    while insert_at > 0 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new_lines = [*lines[:insert_at], item_text, *lines[insert_at:]]
    new_text = "".join(new_lines)
    return new_text, YamlDiff(
        fromLine=insert_at + 1,
        toLine=insert_at,
        replacement=item_text,
    )


def render_delete_item(
    lines: list[str],
    item_start: int,
    item_end: int,
) -> tuple[str, YamlDiff]:
    """Remove the line range covering a single api-action list item."""
    new_lines = [*lines[:item_start], *lines[item_end:]]
    return "".join(new_lines), YamlDiff(
        fromLine=item_start + 1,
        toLine=item_end,
        replacement="",
    )


def render_delete_actions_key(
    lines: list[str],
    actions_start: int,
    actions_end: int,
) -> tuple[str, YamlDiff]:
    """Remove the entire ``actions:`` key when its last item is being dropped."""
    new_lines = [*lines[:actions_start], *lines[actions_end:]]
    return "".join(new_lines), YamlDiff(
        fromLine=actions_start + 1,
        toLine=actions_end,
        replacement="",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _find_block_key_line(
    lines: list[str],
    api_span: tuple[int, int, str],
    keys: tuple[str, ...],
) -> tuple[int, str] | None:
    """Return ``(line index, key)`` of the first block key matching a spelling in *keys*."""
    api_start, api_end, child_indent = api_span
    for key in keys:
        header = f"{child_indent}{key}:"
        for idx in range(api_start + 1, api_end):
            text = lines[idx].rstrip("\n\r")
            if text == header or text.startswith(header + " "):
                return idx, key
    return None


@lru_cache(maxsize=64)
def _discriminator_patterns(
    item_indent: str, keys: tuple[str, ...]
) -> tuple[re.Pattern, re.Pattern]:
    """Return the compiled ``(inline, child)`` discriminator patterns for *keys*."""
    child_indent = item_indent + "  "
    key_alt = "|".join(re.escape(key) for key in keys)
    return (
        re.compile(rf"^{re.escape(item_indent)}-\s*(?:{key_alt}):\s*(?P<val>\S+)"),
        re.compile(rf"^{re.escape(child_indent)}(?:{key_alt}):\s*(?P<val>\S+)"),
    )


def _discriminator(
    lines: list[str],
    item_start: int,
    item_end: int,
    item_indent: str,
    keys: tuple[str, ...],
) -> str | None:
    """Read a list item's discriminator value under any spelling in *keys*."""
    inline_re, child_re = _discriminator_patterns(item_indent, keys)
    inline = inline_re.match(lines[item_start].rstrip("\n\r"))
    if inline:
        return inline.group("val").strip("'\"")
    for idx in range(item_start, item_end):
        m = child_re.match(lines[idx].rstrip("\n\r"))
        if m:
            return m.group("val").strip("'\"")
    return None
