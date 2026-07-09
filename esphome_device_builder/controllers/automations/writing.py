"""
:class:`AutomationTree` → YAML + splice diff.

Top-level ``script:`` / ``interval:`` / ``esphome.on_*`` route
through :func:`helpers.yaml._splice_into_domain_block`; inline
``on_*:`` handlers and light ``effects:`` entries route through
:func:`helpers.yaml.upsert_inline_handler` so adjacent siblings are
left untouched. Delete is the inverse splice.

Trigger handlers always emit the explicit ``then:`` form — the
parser accepts both shortcut forms but emitting one shape keeps
round-trips deterministic. Untagged lambdas render as ruamel
:class:`LiteralScalarString` block scalars; an ``!lambda``-tagged
value re-emits its tag (see :func:`emitter.encode_value`).
"""

from __future__ import annotations

from ...helpers.api import CommandError
from ...helpers.yaml import (
    SubEntityRef,
    _block_end,
    _indent_block,
    _splice_into_domain_block,
    remove_inline_handler,
    remove_subentity_handler,
    upsert_inline_handler,
    upsert_subentity_handler,
)
from ...models.api import ErrorCode
from ...models.automations import (
    ApiActionLocation,
    AutomationLocation,
    AutomationTree,
    AutomationTrigger,
    ComponentActionFieldLocation,
    ComponentOnLocation,
    DeviceOnLocation,
    IntervalLocation,
    LightEffectLocation,
    ScriptLocation,
    YamlDiff,
)
from . import api_actions, catalog
from .emitter import (
    dump,
    emit_trigger_list_item,
    render_action_field,
    render_api_action_item,
    render_interval_item,
    render_script_item,
    render_trigger_handler,
)
from .parsing import (
    ComponentTarget,
    make_yaml,
    resolve_component_domain,
    resolve_component_target,
)
from .writing_layout import (
    _build_diff_for_append,
    _indent_for_top_list,
    _locate_singleton_block,
    _locate_top_list_item,
)
from .writing_lists import (
    ListContainerStrategy,
    delete_light_effect,
    delete_list_entry,
    delete_list_entry_for,
    delete_subentity_list_entry,
    upsert_component_on_entry,
    upsert_light_effect,
    upsert_list_entry,
    upsert_subentity_on_entry,
    wrap_handler_list_block,
)

# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def render_upsert(  # noqa: PLR0911 — one return per location kind; a dispatch table
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
    if isinstance(location, ComponentActionFieldLocation):
        return _upsert_component_action(yaml_text, tree, location)
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
    if isinstance(location, ComponentActionFieldLocation):
        return _delete_component_action(yaml_text, location)
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
    """Splice a device-level ``on_*:`` handler under ``esphome:`` (mapping or list entry)."""
    if location.index is not None:
        return _upsert_device_on_entry(yaml_text, tree, location.trigger, location.index)
    rendered = render_trigger_handler(tree, key=location.trigger)
    return _upsert_under_top_key(yaml_text, "esphome", location.trigger, rendered)


def _esphome_container(yaml_text: str, _error_code: ErrorCode) -> dict | None:
    """Return the ``esphome:`` mapping, or ``None`` when absent (never raises)."""
    data = make_yaml().load(yaml_text) or {}
    esphome = data.get("esphome") if isinstance(data, dict) else None
    return esphome if isinstance(esphome, dict) else None


def _esphome_resplice(yaml_text: str, handler_key: str, entries: list) -> tuple[str, YamlDiff]:
    """Re-emit a device handler list under ``esphome:``; remove the key when emptied."""
    if entries:
        rendered = wrap_handler_list_block(handler_key, dump(entries))
        return _upsert_under_top_key(yaml_text, "esphome", handler_key, rendered)
    return _delete_under_top_key(yaml_text, "esphome", handler_key)


def _esphome_not_present_msg(key: str, index: int) -> str:
    """Delete not-found message for a device-level list handler."""
    return f"{key}[{index}] not present"


_DEVICE_STRATEGY = ListContainerStrategy(
    locate=_esphome_container,
    resplice=_esphome_resplice,
    not_present_msg=_esphome_not_present_msg,
)


def _upsert_device_on_entry(
    yaml_text: str,
    tree: AutomationTree,
    trigger: str,
    index: int,
) -> tuple[str, YamlDiff]:
    """Insert or replace one entry of a list-form device handler (``esphome.on_boot``).

    Refuses to grow a single mapping into a list (the user picked that shape).
    No ``supports_list`` gate here: every device-level trigger is list-capable,
    and the wizard already withholds the affordance for any that are not.
    """
    return upsert_list_entry(
        yaml_text,
        key=trigger,
        item=emit_trigger_list_item(tree),
        index=index,
        strategy=_DEVICE_STRATEGY,
        trigger=catalog.trigger_by_id(trigger),
    )


def _delete_device_on_entry(
    yaml_text: str,
    trigger: str,
    index: int,
) -> tuple[str, YamlDiff]:
    """Drop one entry of a list-form device handler; remove the key when emptied."""
    return delete_list_entry_for(
        yaml_text,
        key=trigger,
        index=index,
        strategy=_DEVICE_STRATEGY,
    )


def _upsert_component_on(
    yaml_text: str,
    tree: AutomationTree,
    location: ComponentOnLocation,
) -> tuple[str, YamlDiff]:
    """Splice an inline ``on_*:`` handler under a configured component."""
    target = resolve_component_target(yaml_text, location.component_id)
    if target is not None and target.is_sub_entity:
        return _upsert_subentity_on(yaml_text, tree, location, target)
    instance_domain = target.domain if target is not None else _component_domain(location)
    trigger = _require_trigger(instance_domain, location)
    if location.index is not None:
        return upsert_component_on_entry(
            yaml_text,
            tree=tree,
            domain=instance_domain,
            component_id=location.component_id,
            trigger_key=location.trigger,
            trigger=trigger,
            index=location.index,
        )
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


def _upsert_subentity_on(
    yaml_text: str,
    tree: AutomationTree,
    location: ComponentOnLocation,
    target: ComponentTarget,
) -> tuple[str, YamlDiff]:
    """Splice an ``on_*:`` handler under a nested sub-entity (``aht20_temperature``)."""
    ref = _subentity_context(target)
    trigger = _require_trigger(target.domain, location)
    if location.index is not None:
        return upsert_subentity_on_entry(
            yaml_text,
            ref,
            tree=tree,
            component_id=location.component_id,
            trigger_key=location.trigger,
            trigger=trigger,
            index=location.index,
        )
    rendered = render_trigger_handler(tree, key=location.trigger)
    res = upsert_subentity_handler(
        yaml_text, ref, handler_key=location.trigger, rendered_yaml=rendered
    )
    if res is None:
        msg = (
            f"Sub-entity id={location.component_id!r} not found under "
            f"{ref.parent_domain!r}; can't splice handler {location.trigger!r}"
        )
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    new_text, from_line, to_line, replacement = res
    return new_text, YamlDiff(fromLine=from_line, toLine=to_line, replacement=replacement)


def _upsert_component_action(
    yaml_text: str,
    tree: AutomationTree,
    location: ComponentActionFieldLocation,
) -> tuple[str, YamlDiff]:
    """Splice an action-list config field (``open_action:`` …) on a component.

    Reuses the same inline-handler splice as ``on_*`` handlers, keyed on
    the literal ``field`` name; only the rendered body differs (a bare
    action list, no ``then:`` wrapper).
    """
    domain = resolve_component_domain(yaml_text, location.component_id)
    if domain is None:
        msg = (
            f"Component instance id={location.component_id!r} not found; "
            f"can't splice action field {location.field!r}"
        )
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    rendered = render_action_field(tree, key=location.field)
    res = upsert_inline_handler(
        yaml_text,
        component_domain=domain,
        component_id=location.component_id,
        handler_key=location.field,
        rendered_yaml=rendered,
    )
    if res is None:
        msg = (
            f"Component instance id={location.component_id!r} not found "
            f"under {domain!r}; can't splice action field {location.field!r}"
        )
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    new_text, from_line, to_line, replacement = res
    return new_text, YamlDiff(fromLine=from_line, toLine=to_line, replacement=replacement)


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
            handler_end = _block_end(lines, idx, end, indent)
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
        if location.index is not None:
            return _delete_device_on_entry(yaml_text, location.trigger, location.index)
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
            handler_end = _block_end(lines, idx, end, indent)
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
    target = resolve_component_target(yaml_text, location.component_id)
    if target is not None and target.is_sub_entity:
        return _delete_subentity_on(yaml_text, location, target)
    instance_domain = target.domain if target is not None else _component_domain(location)
    if location.index is not None:
        return delete_list_entry(
            yaml_text,
            domain=instance_domain,
            component_id=location.component_id,
            handler_key=location.trigger,
            index=location.index,
        )
    trigger = catalog.trigger_by_id(f"{instance_domain}.{location.trigger}")
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


def _delete_subentity_on(
    yaml_text: str,
    location: ComponentOnLocation,
    target: ComponentTarget,
) -> tuple[str, YamlDiff]:
    """Drop an ``on_*:`` handler from a nested sub-entity (``aht20_temperature``)."""
    ref = _subentity_context(target)
    if location.index is not None:
        return delete_subentity_list_entry(
            yaml_text,
            ref,
            component_id=location.component_id,
            handler_key=location.trigger,
            index=location.index,
        )
    res = remove_subentity_handler(yaml_text, ref, handler_key=location.trigger)
    if res is None:
        msg = (
            f"Sub-entity id={location.component_id!r} not found under "
            f"{ref.parent_domain!r}; can't delete handler {location.trigger!r}"
        )
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    new_text, from_line, to_line = res
    return new_text, YamlDiff(fromLine=from_line, toLine=to_line, replacement="")


def _delete_component_action(
    yaml_text: str,
    location: ComponentActionFieldLocation,
) -> tuple[str, YamlDiff]:
    """Drop an action-list config field (``open_action:`` …) from a component."""
    domain = resolve_component_domain(yaml_text, location.component_id)
    if domain is None:
        msg = (
            f"Component instance id={location.component_id!r} not found; "
            f"can't delete action field {location.field!r}"
        )
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    res = remove_inline_handler(
        yaml_text,
        component_domain=domain,
        component_id=location.component_id,
        handler_key=location.field,
    )
    if res is None:
        msg = (
            f"Component instance id={location.component_id!r} not found "
            f"under {domain!r}; can't delete action field {location.field!r}"
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


def _require_trigger(domain: str, location: ComponentOnLocation) -> AutomationTrigger:
    """Look up the catalog trigger for ``<domain>.<trigger>``; raise if unknown."""
    trigger = catalog.trigger_by_id(f"{domain}.{location.trigger}")
    if trigger is None:
        msg = f"Unknown trigger id {location.trigger!r} on component {location.component_id!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return trigger


def _subentity_context(target: ComponentTarget) -> SubEntityRef:
    """Parent context for a sub-entity target; raise if it's incomplete."""
    if target.parent_domain is None or target.parent_id is None or target.sub_key is None:
        msg = f"sub-entity target is missing parent context: {target!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    return SubEntityRef(target.parent_domain, target.parent_id, target.sub_key)


def _component_domain(location: ComponentOnLocation) -> str:
    """Return the inferred domain from a ComponentOnLocation.

    The location object carries ``component_id`` (a YAML id) and a
    trigger key, but not a domain. The trigger catalog maps the
    trigger key + domain to a full id; we resolve by enumerating
    every domain a known trigger of that key applies to and picking
    the first one. ``binary_sensor.on_press`` and ``switch.on_press``
    don't collide because their applies_to lists are disjoint.

    Catalog-only fallback for when ``location.component_id`` isn't found in
    the YAML (``resolve_component_target`` returned ``None``); the writer
    prefers the resolved instance's actual domain when it has one. Picking
    alphabetically here can mis-attribute a shared trigger key (``on_turn_on``
    on ``fan`` vs ``switch``), but the upsert then surfaces a clear
    "id not found" error.
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
