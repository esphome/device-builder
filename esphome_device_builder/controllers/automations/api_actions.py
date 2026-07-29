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

from ...models.automations import YamlDiff

#: Accepted spellings, canonical first — esphome's ``cv.rename_key``
#: aliases in ``components/api``. The catalog sync fails when upstream
#: adds a rename outside its handled list, so new pairs land here
#: deliberately, with a migration helper.
BLOCK_KEYS = ("actions", "services")
ITEM_KEYS = ("action", "service")

_ITEM_KEY_ALT = "|".join(re.escape(key) for key in ITEM_KEYS)
_LEGACY_ITEM_KEY_ALT = "|".join(re.escape(key) for key in ITEM_KEYS[1:])
#: Discriminator lines with the indent captured; callers compare the
#: ``indent`` group against the expected column so a same-named key
#: nested deeper in an action body can't match.
_INLINE_DISCRIMINATOR_RE = re.compile(rf"^(?P<indent>[ ]*)-\s*(?:{_ITEM_KEY_ALT}):\s*(?P<val>\S+)")
_CHILD_DISCRIMINATOR_RE = re.compile(rf"^(?P<indent>[ ]*)(?:{_ITEM_KEY_ALT}):\s*(?P<val>\S+)")


def inline_actions_key(
    lines: list[str],
    api_span: tuple[int, int, str],
) -> str | None:
    """Return the block key under *api_span* carrying an inline value, if any.

    Flow-style values (``actions: []``, ``actions: null``,
    ``actions: !secret foo``, …) can't be spliced into the way the
    line-based writer wants. Callers should refuse the upsert /
    delete and surface a clear error rather than emit a second
    key alongside the inline one. Both spellings are checked in full.
    """
    api_start, api_end, child_indent = api_span
    for key in BLOCK_KEYS:
        header = f"{child_indent}{key}:"
        for idx in range(api_start + 1, api_end):
            text = lines[idx].rstrip("\n\r")
            if text == header:
                break
            if text.startswith(header + " "):
                rest = text[len(header) :].strip()
                if rest and not rest.startswith("#"):
                    return key
                break
    return None


def locate_actions_list(
    lines: list[str],
    api_span: tuple[int, int, str],
) -> tuple[int, int, str, str] | None:
    """Return ``(start, end, item_indent, key)`` for the api action list or ``None``.

    Matches either block spelling, canonical winning when both appear;
    ``start`` is the line index of the block key; ``end`` is one past
    the last item line; ``item_indent`` is the leading whitespace
    shared by each ``- ...`` dash line; ``key`` is the spelling in the
    file.
    """
    _api_start, api_end, child_indent = api_span
    found = _find_block_key_line(lines, api_span)
    if found is None:
        return None
    actions_start, matched_key = found
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
    return actions_start, actions_end, item_indent, matched_key


def canonicalize_block(
    lines: list[str],
    actions_start: int,
    actions_end: int,
    item_indent: str,
    matched_key: str,
) -> list[str]:
    """
    Respell the block key and item discriminators to canonical, line for line.

    Only the key tokens change; discriminators are matched on the dash
    line or at the item's child indent only, so a same-named key inside
    an action body stays untouched. An item already carrying the
    canonical discriminator keeps its legacy one.
    """
    out = list(lines)
    out[actions_start] = re.sub(
        rf"^(\s*){re.escape(matched_key)}:", rf"\g<1>{BLOCK_KEYS[0]}:", out[actions_start], count=1
    )
    item_re = re.compile(rf"^({re.escape(item_indent)}(?:-\s*|  ))(?:{_LEGACY_ITEM_KEY_ALT}):")
    canonical_re = re.compile(rf"^{re.escape(item_indent)}(?:-\s*|  ){ITEM_KEYS[0]}:")
    for start, end in _item_spans(out, actions_start, actions_end, item_indent):
        if any(canonical_re.match(out[idx].rstrip("\n\r")) for idx in range(start, end)):
            continue
        for idx in range(start, end):
            out[idx] = item_re.sub(rf"\g<1>{ITEM_KEYS[0]}:", out[idx], count=1)
    return out


def find_item(
    lines: list[str],
    actions_start: int,
    actions_end: int,
    item_indent: str,
    action_name: str,
) -> tuple[int, int] | None:
    """Locate the line range of the list item whose discriminator matches."""
    return next(
        (
            span
            for span in _item_spans(lines, actions_start, actions_end, item_indent)
            if _discriminator(lines, *span, item_indent) == action_name
        ),
        None,
    )


def count_siblings(
    lines: list[str],
    actions_start: int,
    actions_end: int,
    item_indent: str,
    matched: tuple[int, int],
) -> int:
    """Count list items at *item_indent* that aren't the matched span."""
    item_start, item_end = matched
    return sum(
        1
        for start, _end in _item_spans(lines, actions_start, actions_end, item_indent)
        if not item_start <= start < item_end
    )


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


def render_create_block(yaml_text: str, rendered: str) -> tuple[str, str]:
    """Return ``(new_yaml, block_text)`` for a fresh ``api:`` block at EOF."""
    item_indent = "    "
    item_text = indent_for_list(rendered, item_indent)
    # ``item_text`` always ends with ``\n``, so the block does too and
    # the final concatenation lands a trailing newline on EOF.
    block = "api:\n  actions:\n" + item_text
    base = yaml_text.rstrip()
    separator = "\n\n" if base else ""
    return f"{base}{separator}{block}", block


def render_insert_actions_key(
    lines: list[str],
    api_span: tuple[int, int, str],
    rendered: str,
) -> tuple[str, YamlDiff]:
    """Insert an ``actions:`` key with one item under an existing ``api:`` block."""
    api_start, api_end, child_indent = api_span
    item_indent = child_indent + "  "
    item_text = indent_for_list(rendered, item_indent)
    block = f"{child_indent}actions:\n{item_text}"
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


def _item_spans(
    lines: list[str],
    actions_start: int,
    actions_end: int,
    item_indent: str,
) -> list[tuple[int, int]]:
    """Return each list item's ``(start, end)`` line span within the block."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for idx in range(actions_start + 1, actions_end):
        if lines[idx].rstrip("\n\r").startswith(item_indent + "- "):
            if start is not None:
                spans.append((start, idx))
            start = idx
    if start is not None:
        spans.append((start, actions_end))
    return spans


def _find_block_key_line(
    lines: list[str],
    api_span: tuple[int, int, str],
) -> tuple[int, str] | None:
    """Return ``(line index, key)`` of the first block key matching either spelling."""
    api_start, api_end, child_indent = api_span
    for key in BLOCK_KEYS:
        header = f"{child_indent}{key}:"
        for idx in range(api_start + 1, api_end):
            text = lines[idx].rstrip("\n\r")
            if text == header or text.startswith(header + " "):
                return idx, key
    return None


def _discriminator(
    lines: list[str],
    item_start: int,
    item_end: int,
    item_indent: str,
) -> str | None:
    """Read the ``action:`` (or legacy ``service:``) key for a list item."""
    child_indent = item_indent + "  "
    inline = _INLINE_DISCRIMINATOR_RE.match(lines[item_start].rstrip("\n\r"))
    if inline and inline.group("indent") == item_indent:
        return inline.group("val").strip("'\"")
    for idx in range(item_start, item_end):
        m = _CHILD_DISCRIMINATOR_RE.match(lines[idx].rstrip("\n\r"))
        if m and m.group("indent") == child_indent:
            return m.group("val").strip("'\"")
    return None
