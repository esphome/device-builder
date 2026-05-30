"""
:class:`AutomationTree` → YAML + splice diff.

Top-level ``script:`` / ``interval:`` / ``esphome.on_*`` route
through :func:`helpers.yaml._splice_into_domain_block`; inline
``on_*:`` handlers and light ``effects:`` entries route through
:func:`helpers.yaml.upsert_inline_handler` so adjacent siblings are
left untouched. Delete is the inverse splice.

Trigger handlers always emit the explicit ``then:`` form — the
parser accepts both shortcut forms but emitting one shape keeps
round-trips deterministic. Lambdas render as ruamel
:class:`LiteralScalarString` block scalars.
"""

from __future__ import annotations

import re

from ...helpers.api import CommandError
from ...helpers.yaml import (
    _splice_into_domain_block,
    remove_inline_handler,
    upsert_inline_handler,
)
from ...models.api import ErrorCode
from ...models.automations import (
    ApiActionLocation,
    AutomationLocation,
    AutomationTree,
    ComponentOnLocation,
    DeviceOnLocation,
    IntervalLocation,
    LightEffectLocation,
    ScriptLocation,
    YamlDiff,
)
from . import api_actions, catalog
from .emitter import (
    render_api_action_item,
    render_interval_item,
    render_script_item,
    render_trigger_handler,
)
from .parsing import make_yaml
from .writing_lists import delete_light_effect, upsert_light_effect

# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def render_upsert(
    yaml_text: str,
    *,
    tree: AutomationTree,
    location: AutomationLocation,
) -> tuple[str, YamlDiff]:
    """
    Apply *tree* at *location*; return ``(new_yaml, diff)``.

    *diff* is the :class:`YamlDiff` splice the frontend applies to
    the editor pane. *new_yaml* is the post-splice document — caller
    convenience so tests and callers don't re-derive it.
    """
    if isinstance(location, ScriptLocation):
        return _upsert_script(yaml_text, tree, location)
    if isinstance(location, IntervalLocation):
        return _upsert_interval(yaml_text, tree, location)
    if isinstance(location, DeviceOnLocation):
        return _upsert_device_on(yaml_text, tree, location)
    if isinstance(location, ComponentOnLocation):
        return _upsert_component_on(yaml_text, tree, location)
    if isinstance(location, LightEffectLocation):
        return upsert_light_effect(yaml_text, tree, location)
    if isinstance(location, ApiActionLocation):
        return _upsert_api_action(yaml_text, tree, location)
    msg = f"Unsupported AutomationLocation: {type(location).__name__}"
    raise CommandError(ErrorCode.INVALID_ARGS, msg)


def render_delete(
    yaml_text: str,
    *,
    location: AutomationLocation,
) -> tuple[str, YamlDiff]:
    """Remove the automation at *location*; return ``(new_yaml, diff)``."""
    if isinstance(location, (ScriptLocation, IntervalLocation, DeviceOnLocation)):
        return _delete_top_level(yaml_text, location)
    if isinstance(location, ComponentOnLocation):
        return _delete_component_on(yaml_text, location)
    if isinstance(location, LightEffectLocation):
        return delete_light_effect(yaml_text, location)
    if isinstance(location, ApiActionLocation):
        return _delete_api_action(yaml_text, location)
    msg = f"Unsupported AutomationLocation: {type(location).__name__}"
    raise CommandError(ErrorCode.INVALID_ARGS, msg)


# ---------------------------------------------------------------------------
# Per-location upsert paths
# ---------------------------------------------------------------------------


def _upsert_script(
    yaml_text: str,
    tree: AutomationTree,
    location: ScriptLocation,
) -> tuple[str, YamlDiff]:
    """Splice or replace a top-level ``script:`` list item."""
    rendered = render_script_item(tree, location.id)
    return _upsert_top_level_list(yaml_text, "script", rendered, location.id, "id")


def _upsert_interval(
    yaml_text: str,
    tree: AutomationTree,
    location: IntervalLocation,
) -> tuple[str, YamlDiff]:
    """Splice or replace a top-level ``interval:`` list item by index."""
    rendered = render_interval_item(tree)
    return _upsert_top_level_list_indexed(yaml_text, "interval", rendered, location.index)


def _upsert_device_on(
    yaml_text: str,
    tree: AutomationTree,
    location: DeviceOnLocation,
) -> tuple[str, YamlDiff]:
    """Splice a device-level ``on_*:`` handler under ``esphome:``."""
    rendered = render_trigger_handler(tree, key=location.trigger)
    return _upsert_under_top_key(yaml_text, "esphome", location.trigger, rendered)


def _upsert_component_on(
    yaml_text: str,
    tree: AutomationTree,
    location: ComponentOnLocation,
) -> tuple[str, YamlDiff]:
    """Splice an inline ``on_*:`` handler under a configured component."""
    trigger = catalog.trigger_by_id(
        f"{_component_domain_from_yaml(yaml_text, location)}.{location.trigger}",
    )
    if trigger is None:
        msg = f"Unknown trigger id {location.trigger!r} on component {location.component_id!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    domain = trigger.applies_to[0] if trigger.applies_to else ""
    rendered = render_trigger_handler(tree, key=location.trigger)
    res = upsert_inline_handler(
        yaml_text,
        component_domain=domain,
        component_id=location.component_id,
        handler_key=location.trigger,
        rendered_yaml=rendered,
    )
    if res is None:
        msg = (
            f"Component instance id={location.component_id!r} not found "
            f"under {domain!r}; can't splice handler {location.trigger!r}"
        )
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    new_text, from_line, to_line, replacement = res
    return new_text, YamlDiff(
        fromLine=from_line,
        toLine=to_line,
        replacement=replacement,
    )


def _upsert_api_action(
    yaml_text: str,
    tree: AutomationTree,
    location: ApiActionLocation,
) -> tuple[str, YamlDiff]:
    """Splice or replace an ``api.actions:`` list item by ``action_name``."""
    rendered = render_api_action_item(tree, location.action_name)
    lines = yaml_text.splitlines(keepends=True)
    api_span = _locate_singleton_block(lines, "api")
    if api_span is None:
        new_text, _block = api_actions.render_create_block(yaml_text, rendered)
        return new_text, _build_diff_for_append(yaml_text, new_text)
    if api_actions.has_inline_actions_value(lines, api_span):
        msg = "api.actions: is inline (e.g. `actions: []`); rewrite it as a block list first"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    actions_span = api_actions.locate_actions_list(lines, api_span)
    if actions_span is None:
        return api_actions.render_insert_actions_key(lines, api_span, rendered)
    actions_start, actions_end, item_indent = actions_span
    existing = api_actions.find_item(
        lines,
        actions_start,
        actions_end,
        item_indent,
        location.action_name,
    )
    if existing is not None:
        item_start, item_end = existing
        rendered_text = api_actions.indent_for_list(rendered, item_indent)
        return api_actions.render_replacement(lines, item_start, item_end, rendered_text)
    return api_actions.render_append(lines, actions_end, item_indent, rendered)


# ---------------------------------------------------------------------------
# Top-level list splice helpers
# ---------------------------------------------------------------------------


def _upsert_top_level_list(
    yaml_text: str,
    domain: str,
    rendered_item: str,
    item_id: str,
    id_key: str,
) -> tuple[str, YamlDiff]:
    """Insert / replace a list item identified by a string id field."""
    yaml = make_yaml()
    data = yaml.load(yaml_text) or {}
    items = data.get(domain) if isinstance(data, dict) else None
    existing_idx: int | None = None
    if isinstance(items, list):
        for idx, raw in enumerate(items):
            if isinstance(raw, dict) and str(raw.get(id_key, "")) == item_id:
                existing_idx = idx
                break
    if existing_idx is None:
        return _append_top_level_list(yaml_text, domain, rendered_item)
    return _replace_top_level_list_item(yaml_text, domain, existing_idx, rendered_item)


def _upsert_top_level_list_indexed(
    yaml_text: str,
    domain: str,
    rendered_item: str,
    index: int,
) -> tuple[str, YamlDiff]:
    """Insert (at the end) or replace a list item by positional index."""
    yaml = make_yaml()
    data = yaml.load(yaml_text) or {}
    items = data.get(domain) if isinstance(data, dict) else None
    if isinstance(items, list) and 0 <= index < len(items):
        return _replace_top_level_list_item(yaml_text, domain, index, rendered_item)
    return _append_top_level_list(yaml_text, domain, rendered_item)


def _append_top_level_list(
    yaml_text: str,
    domain: str,
    rendered_item: str,
) -> tuple[str, YamlDiff]:
    """Append *rendered_item* under ``<domain>:`` (creating the block if needed)."""
    block = f"{domain}:\n{rendered_item.rstrip()}\n"
    spliced = _splice_into_domain_block(yaml_text, domain, block)
    if spliced is None:
        # Append a fresh top-level block at end-of-file.
        base = yaml_text.rstrip()
        separator = "\n\n" if base else ""
        spliced = f"{base}{separator}{block}"
    diff = _build_diff_for_append(yaml_text, spliced)
    return spliced, diff


def _replace_top_level_list_item(
    yaml_text: str,
    domain: str,
    index: int,
    rendered_item: str,
) -> tuple[str, YamlDiff]:
    """Replace the *index*'th list item under ``<domain>:`` with rendered_item."""
    lines = yaml_text.splitlines(keepends=True)
    start, end = _locate_top_list_item(lines, domain, index)
    indented = _indent_for_top_list(rendered_item)
    new_lines = [*lines[:start], indented, *lines[end:]]
    new_text = "".join(new_lines)
    return new_text, YamlDiff(
        fromLine=start + 1,
        toLine=end,
        replacement=indented,
    )


def _upsert_under_top_key(
    yaml_text: str,
    block_key: str,
    handler_key: str,
    rendered_yaml: str,
) -> tuple[str, YamlDiff]:
    """Splice ``<handler_key>:`` under a singleton block (``esphome:``)."""
    lines = yaml_text.splitlines(keepends=True)
    span = _locate_singleton_block(lines, block_key)
    if span is None:
        # Block doesn't exist — append both block and handler.
        rendered_lines = _indent_block(rendered_yaml, "  ")
        block = f"{block_key}:\n" + "\n".join(rendered_lines) + "\n"
        base = yaml_text.rstrip()
        separator = "\n\n" if base else ""
        new_text = f"{base}{separator}{block}"
        diff = _build_diff_for_append(yaml_text, new_text)
        return new_text, diff
    start, end, indent = span
    handler_re_prefix = f"{indent}{handler_key}:"
    handler_start: int | None = None
    handler_end: int | None = None
    for idx in range(start + 1, end):
        text = lines[idx].rstrip("\n\r")
        if text == handler_re_prefix or text.startswith(handler_re_prefix + " "):
            handler_start = idx
            for jdx in range(idx + 1, end):
                content = lines[jdx].rstrip("\n\r")
                if not content:
                    continue
                leading = len(content) - len(content.lstrip(" "))
                if leading <= len(indent):
                    handler_end = jdx
                    break
            if handler_end is None:
                handler_end = end
            break
    rendered_text = "\n".join(_indent_block(rendered_yaml, indent)) + "\n"
    if handler_start is not None and handler_end is not None:
        new_lines = [*lines[:handler_start], rendered_text, *lines[handler_end:]]
        new_text = "".join(new_lines)
        return new_text, YamlDiff(
            fromLine=handler_start + 1,
            toLine=handler_end,
            replacement=rendered_text,
        )
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    new_lines = [*lines[:insert_at], rendered_text, *lines[insert_at:]]
    new_text = "".join(new_lines)
    # Pure-insert convention: ``toLine == fromLine - 1`` encodes
    # "no lines replaced; insert before fromLine". See
    # :class:`YamlDiff`'s docstring.
    return new_text, YamlDiff(
        fromLine=insert_at + 1,
        toLine=insert_at,
        replacement=rendered_text,
    )


# ---------------------------------------------------------------------------
# Delete paths
# ---------------------------------------------------------------------------


def _delete_top_level(
    yaml_text: str,
    location: AutomationLocation,
) -> tuple[str, YamlDiff]:
    """Drop a top-level script / interval / device-on block."""
    if isinstance(location, ScriptLocation):
        return _delete_top_level_list_by_id(
            yaml_text,
            "script",
            "id",
            location.id,
        )
    if isinstance(location, IntervalLocation):
        return _delete_top_level_list_by_index(yaml_text, "interval", location.index)
    if isinstance(location, DeviceOnLocation):
        return _delete_under_top_key(yaml_text, "esphome", location.trigger)
    # Unreachable when called from ``render_delete`` (the dispatch
    # there only forwards the three union members above). Kept as
    # a defensive guard against future location types being added
    # without updating both dispatchers.
    msg = f"Unsupported delete location: {type(location).__name__}"  # pragma: no cover
    raise CommandError(ErrorCode.INVALID_ARGS, msg)  # pragma: no cover


def _delete_top_level_list_by_id(
    yaml_text: str,
    domain: str,
    id_key: str,
    item_id: str,
) -> tuple[str, YamlDiff]:
    """Remove the list item under ``<domain>:`` whose ``id`` matches."""
    yaml = make_yaml()
    data = yaml.load(yaml_text) or {}
    items = data.get(domain) if isinstance(data, dict) else None
    if not isinstance(items, list):
        msg = f"Block {domain!r} not present; nothing to delete"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    for idx, raw in enumerate(items):
        if isinstance(raw, dict) and str(raw.get(id_key, "")) == item_id:
            return _delete_top_level_list_by_index(yaml_text, domain, idx)
    msg = f"{domain}:[{id_key}={item_id!r}] not present"
    raise CommandError(ErrorCode.NOT_FOUND, msg)


def _delete_top_level_list_by_index(
    yaml_text: str,
    domain: str,
    index: int,
) -> tuple[str, YamlDiff]:
    """Remove the *index*'th list item under ``<domain>:``."""
    lines = yaml_text.splitlines(keepends=True)
    start, end = _locate_top_list_item(lines, domain, index)
    new_lines = [*lines[:start], *lines[end:]]
    new_text = "".join(new_lines)
    return new_text, YamlDiff(
        fromLine=start + 1,
        toLine=end,
        replacement="",
    )


def _delete_under_top_key(
    yaml_text: str,
    block_key: str,
    handler_key: str,
) -> tuple[str, YamlDiff]:
    """Remove ``<handler_key>:`` from under ``<block_key>:``."""
    lines = yaml_text.splitlines(keepends=True)
    span = _locate_singleton_block(lines, block_key)
    if span is None:
        msg = f"Block {block_key!r} not present; nothing to delete"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    start, end, indent = span
    handler_prefix = f"{indent}{handler_key}:"
    for idx in range(start + 1, end):
        text = lines[idx].rstrip("\n\r")
        if text == handler_prefix or text.startswith(handler_prefix + " "):
            handler_end = end
            for jdx in range(idx + 1, end):
                content = lines[jdx].rstrip("\n\r")
                if not content:
                    continue
                leading = len(content) - len(content.lstrip(" "))
                if leading <= len(indent):
                    handler_end = jdx
                    break
            new_lines = [*lines[:idx], *lines[handler_end:]]
            return "".join(new_lines), YamlDiff(
                fromLine=idx + 1,
                toLine=handler_end,
                replacement="",
            )
    msg = f"{block_key}.{handler_key} not present"
    raise CommandError(ErrorCode.NOT_FOUND, msg)


def _delete_component_on(
    yaml_text: str,
    location: ComponentOnLocation,
) -> tuple[str, YamlDiff]:
    """Drop an inline ``on_*:`` handler from a configured component."""
    trigger = catalog.trigger_by_id(
        f"{_component_domain_from_yaml(yaml_text, location)}.{location.trigger}",
    )
    domain = trigger.applies_to[0] if trigger and trigger.applies_to else ""
    res = remove_inline_handler(
        yaml_text,
        component_domain=domain,
        component_id=location.component_id,
        handler_key=location.trigger,
    )
    if res is None:
        msg = (
            f"Component instance id={location.component_id!r} not found "
            f"under {domain!r}; can't delete handler {location.trigger!r}"
        )
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    new_text, from_line, to_line = res
    return new_text, YamlDiff(fromLine=from_line, toLine=to_line, replacement="")


def _delete_api_action(
    yaml_text: str,
    location: ApiActionLocation,
) -> tuple[str, YamlDiff]:
    """Drop a single ``api.actions:`` item; drop ``actions:`` when emptied."""
    lines = yaml_text.splitlines(keepends=True)
    api_span = _locate_singleton_block(lines, "api")
    if api_span is None:
        msg = "api: block not present; nothing to delete"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    if api_actions.has_inline_actions_value(lines, api_span):
        msg = "api.actions: is inline (e.g. `actions: []`); rewrite it as a block list first"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    actions_span = api_actions.locate_actions_list(lines, api_span)
    if actions_span is None:
        msg = "api.actions: not present; nothing to delete"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    actions_start, actions_end, item_indent = actions_span
    existing = api_actions.find_item(
        lines,
        actions_start,
        actions_end,
        item_indent,
        location.action_name,
    )
    if existing is None:
        msg = f"api.actions[action={location.action_name!r}] not present"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    item_start, item_end = existing
    siblings = api_actions.count_siblings(
        lines,
        actions_start,
        actions_end,
        item_indent,
        existing,
    )
    if siblings > 0:
        return api_actions.render_delete_item(lines, item_start, item_end)
    # Last sibling — drop the entire ``actions:`` key as well so the
    # file doesn't grow ``actions: []`` noise.
    return api_actions.render_delete_actions_key(lines, actions_start, actions_end)


# ---------------------------------------------------------------------------
# Low-level utilities
# ---------------------------------------------------------------------------


def _component_domain(location: ComponentOnLocation) -> str:
    """Return the inferred domain from a ComponentOnLocation.

    The location object carries ``component_id`` (a YAML id) and a
    trigger key, but not a domain. The trigger catalog maps the
    trigger key + domain to a full id; we resolve by enumerating
    every domain a known trigger of that key applies to and picking
    the first one. ``binary_sensor.on_press`` and ``switch.on_press``
    don't collide because their applies_to lists are disjoint.

    Used as the fallback when the YAML hasn't been provided (no
    ``yaml_text`` in scope) — see :func:`_component_domain_from_yaml`
    for the disambiguated lookup the writer prefers when it has
    the actual YAML.
    """
    matches = [
        t
        for t in catalog.all_triggers()
        if not t.is_device_level and t.id.endswith("." + location.trigger)
    ]
    if not matches:
        return ""
    if len(matches) > 1:
        # Multiple domains share this trigger key. We don't know
        # which one the caller intended; the caller must disambiguate
        # via the trigger.applies_to list on a fully-qualified
        # location. For now pick the alphabetically-first domain so
        # tests are deterministic.
        matches.sort(key=lambda t: t.applies_to[0] if t.applies_to else "")
    return matches[0].applies_to[0] if matches[0].applies_to else ""


def _component_domain_from_yaml(
    yaml_text: str,
    location: ComponentOnLocation,
) -> str:
    """Find the YAML domain that hosts ``location.component_id``.

    Trigger keys like ``on_turn_on`` belong to multiple domains
    (``switch``, ``fan``, ``light``, ``cover``, …). The catalog-only
    fallback in :func:`_component_domain` picks one alphabetically,
    which means a ``relay`` switch instance with an ``on_turn_on``
    handler gets attributed to ``fan`` (alphabetically first), and
    the writer then fails with "instance id='relay' not found
    under 'fan'".

    Walk the YAML and find the top-level key whose subtree contains
    ``id: <component_id>``. That's the domain the user actually
    configured. Falls back to the catalog guess when the id can't
    be located in the YAML (which also means the upsert won't find
    a splice destination — the user will see a clearer
    "id not found" error from ``upsert_inline_handler``).
    """
    target_id = location.component_id
    id_re = re.compile(
        r"^\s+(?:-\s+)?id:\s*[\"']?(\S+?)[\"']?\s*(?:#.*)?$",
    )
    top_re = re.compile(r"^([a-zA-Z_][\w]*)\s*:")
    current_domain: str | None = None
    for line in yaml_text.splitlines():
        if line and not line[0].isspace():
            m = top_re.match(line)
            current_domain = m.group(1) if m else None
            continue
        if current_domain is None:
            continue
        m = id_re.match(line)
        if m and m.group(1) == target_id:
            return current_domain
    return _component_domain(location)


def _indent_block(block_text: str, indent: str) -> list[str]:
    """Prefix every non-empty line of *block_text* with *indent*."""
    out: list[str] = []
    for line in block_text.splitlines():
        if not line:
            out.append("")
            continue
        out.append(indent + line)
    return out


def _indent_for_top_list(rendered_item: str) -> str:
    """Indent *rendered_item* (one ``- ...`` block) for top-level list use."""
    # ``dump([item])`` already produces the dashed list form; we
    # use it as-is. The block is left at column-0 so it lands
    # correctly under any top-level domain.
    if not rendered_item.endswith("\n"):
        rendered_item += "\n"
    return rendered_item


def _locate_top_list_item(  # noqa: C901
    lines: list[str],
    domain: str,
    index: int,
) -> tuple[int, int]:
    """Return the line range of the *index*'th item under ``<domain>:``."""
    domain_start: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.rstrip("\n\r")
        if stripped == f"{domain}:" or stripped.startswith(f"{domain}:"):
            domain_start = idx
            break
    if domain_start is None:
        msg = f"Block {domain!r} not present"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    domain_end = len(lines)
    for idx in range(domain_start + 1, len(lines)):
        stripped = lines[idx].rstrip("\n\r")
        if stripped and stripped[0].isalpha() and not stripped.startswith(" "):
            domain_end = idx
            break
    # Only column-2 dashes count as top-level list items; deeper
    # dashes belong to nested action lists inside the item body.
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
            continue
        item_starts.append(idx)
    if index < 0 or index >= len(item_starts):
        msg = f"{domain}[{index}] out of range (have {len(item_starts)})"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    start = item_starts[index]
    end = item_starts[index + 1] if index + 1 < len(item_starts) else domain_end
    return start, end


def _locate_singleton_block(
    lines: list[str],
    block_key: str,
) -> tuple[int, int, str] | None:
    """Return ``(start, end, child_indent)`` for a singleton mapping block."""
    header = f"{block_key}:"
    start: int | None = None
    indent = "  "
    for idx, line in enumerate(lines):
        stripped = line.rstrip("\n\r")
        if stripped == header or stripped.startswith(header + " "):
            start = idx
            break
    if start is None:
        return None
    end = len(lines)
    captured = False
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].rstrip("\n\r")
        if not stripped:
            continue
        if not stripped.startswith(" "):
            if stripped[0].isalpha():
                end = idx
                break
            # Column-0 comment ends the block only when the next
            # non-blank line is also column-0 (a section banner
            # between two top-level blocks). A comment sitting
            # between a parent key and an indented child below is
            # a no-op — keep scanning.
            if _next_non_blank_at_col_zero(lines, idx + 1):
                end = idx
                break
            continue
        if not captured:
            indent = " " * (len(stripped) - len(stripped.lstrip(" ")))
            captured = True
    return start, end, indent


def _next_non_blank_at_col_zero(lines: list[str], start: int) -> bool:
    """Return True iff the next non-blank line at *start* or later sits at column 0."""
    for idx in range(start, len(lines)):
        stripped = lines[idx].rstrip("\n\r")
        if not stripped:
            continue
        return not stripped.startswith(" ")
    return False


def _build_diff_for_append(old_yaml: str, new_yaml: str) -> YamlDiff:
    """Build a diff describing the lines added by an append-style write.

    Walks both texts to find the first divergent line, then takes
    everything after that on the new side as the inserted range.
    Good enough for the append case where we always grow the file
    at the end (or just after the matched top-level block).
    """
    old_lines = old_yaml.splitlines()
    new_lines = new_yaml.splitlines()
    common = 0
    while (
        common < len(old_lines)
        and common < len(new_lines)
        and old_lines[common] == new_lines[common]
    ):
        common += 1
    from_line = common + 1
    to_line = common  # exclusive; equal start ⇒ pure insert
    replacement = "\n".join(new_lines[common:])
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    return YamlDiff(fromLine=from_line, toLine=to_line, replacement=replacement)


def _extract_replacement(yaml_text: str, from_line: int, to_line: int) -> str:
    """Return the post-splice text spanned by the :class:`YamlDiff` range."""
    lines = yaml_text.splitlines(keepends=True)
    return "".join(lines[from_line - 1 : to_line])
