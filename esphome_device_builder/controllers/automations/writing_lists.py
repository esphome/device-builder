"""
Splice helpers for list-shaped handlers under a component instance.

These handlers are a YAML *list* nested under a configured component
instance — light ``effects:`` today, list-form triggers (``time.on_time``)
next. They share one shape: parse-mutate-reemit the whole list block, then
splice it back through :func:`helpers.yaml.upsert_inline_handler` (or remove
it when emptied). Kept out of ``writing.py`` so that file stays focused on
the single-handler / top-level splice paths.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from ruamel.yaml.comments import CommentedMap

from ...helpers.api import CommandError
from ...helpers.yaml import (
    remove_inline_handler,
    synthetic_instance_index,
    upsert_inline_handler,
)
from ...models.api import ErrorCode
from ...models.automations import (
    AutomationTree,
    AutomationTrigger,
    LightEffectLocation,
    YamlDiff,
)
from . import catalog
from .emitter import dump, emit_effect_item, emit_trigger_list_item
from .parsing import is_trigger_entry, make_yaml

# The YAML field naming a component instance's id.
_ID_KEY = "id"


def wrap_handler_list_block(handler_key: str, rendered_list: str) -> str:
    """Prefix a rendered dashed list with its ``<handler_key>:`` header."""
    # ``upsert_inline_handler`` writes ``rendered_yaml`` verbatim under the
    # component instance; the list dump is bare, so add the header here.
    return f"{handler_key}:\n" + rendered_list.rstrip() + "\n"


def drop_after_block_comment(entries: list) -> None:
    """
    Strip the comments ruamel bound to the last list entry.

    That trailing slot also holds whatever followed the block in the
    source (the next sibling's leading comment); re-emitting it would
    duplicate that comment on splice. The sequence's own comments and
    earlier entries are untouched, so inner comments survive. A comment
    authored at the tail of the last entry is indistinguishable from the
    after-block one, so it's dropped; an accepted cost versus the
    duplication it would otherwise cause.
    """
    if entries:
        _strip_comments(entries[-1])


def _strip_comments(node: Any) -> None:
    """Recursively clear ruamel comment associations on *node*."""
    ca = getattr(node, "ca", None)
    if ca is not None:
        ca.items.clear()
        ca.comment = None
        ca.end.clear()
    if isinstance(node, dict):
        for child in node.values():
            _strip_comments(child)
    elif isinstance(node, list):
        for child in node:
            _strip_comments(child)


def _resplice_list_block(
    yaml_text: str,
    handler_key: str,
    entries: list,
    *,
    domain: str,
    component_id: str,
) -> tuple[str, YamlDiff]:
    """
    Re-emit a component's list handler from *entries* and return the diff.

    Non-empty *entries* replace the whole block; an empty list removes the
    handler key. Callers locate the instance first, so a ``None`` splice
    result is unreachable.
    """
    if entries:
        rendered = wrap_handler_list_block(handler_key, dump(entries))
        res = upsert_inline_handler(
            yaml_text,
            component_domain=domain,
            component_id=component_id,
            handler_key=handler_key,
            rendered_yaml=rendered,
        )
        if res is None:  # pragma: no cover — instance located by the caller
            msg = f"Component instance id={component_id!r} not found under {domain!r}"
            raise CommandError(ErrorCode.INTERNAL_ERROR, msg)
        new_text, from_line, to_line, replacement = res
        return new_text, YamlDiff(fromLine=from_line, toLine=to_line, replacement=replacement)
    removed = remove_inline_handler(
        yaml_text,
        component_domain=domain,
        component_id=component_id,
        handler_key=handler_key,
    )
    if removed is None:  # pragma: no cover — instance located by the caller
        msg = f"{handler_key}: not found on component id={component_id!r}"
        raise CommandError(ErrorCode.INTERNAL_ERROR, msg)
    new_text, from_line, to_line = removed
    return new_text, YamlDiff(fromLine=from_line, toLine=to_line, replacement="")


def apply_list_entry_upsert(entries: list, item: Any, index: int, *, label: str) -> None:
    """Append (``index == len``), replace (in range), or raise (out of range).

    The shared insert-or-replace-at-index step for every list-shaped handler
    (component ``on_*``, light effects, device ``on_*``); mutates *entries*.
    """
    if index == len(entries):
        entries.append(item)
    elif 0 <= index < len(entries):
        entries[index] = item
    else:
        msg = f"{label}[{index}] out of range (have {len(entries)})"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)


@dataclass(frozen=True)
class ListContainerStrategy:
    """How to find the dict holding a handler list and how to re-emit it.

    ``locate`` returns ``None`` only when an absent container should be
    auto-created on resplice (device ``esphome:``); it raises with the
    passed ``error_code`` when the container is a hard prerequisite
    (a configured component instance). ``not_present_msg`` builds the
    delete error.
    """

    locate: Callable[[str, ErrorCode], dict | None]
    resplice: Callable[[str, str, list], tuple[str, YamlDiff]]
    not_present_msg: Callable[[str, int], str]


def upsert_list_entry(
    yaml_text: str,
    *,
    key: str,
    item: Any,
    index: int,
    strategy: ListContainerStrategy,
    trigger: AutomationTrigger | None = None,
) -> tuple[str, YamlDiff]:
    """
    Insert or replace one entry of a list-shaped handler at *index*.

    ``index == len(entries)`` appends; an in-range index replaces. Refuses
    when the existing handler is a single mapping rather than a list — the
    user picked that shape, so don't silently rewrite it.

    When *trigger* is given and the existing list body is the bare
    action-list shorthand (one handler whose items aren't trigger
    entries), wrap it as a single ``then:`` entry before applying, so a
    new handler appends instead of overwriting an action (#1305).
    """
    container = strategy.locate(yaml_text, ErrorCode.INVALID_ARGS)
    existing = container.get(key) if container is not None else None
    if existing is not None and not isinstance(existing, list):
        msg = f"{key}: is a single mapping, not a list; convert it to a list first"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    entries = existing if isinstance(existing, list) else []
    drop_after_block_comment(entries)
    if entries and trigger is not None and not all(is_trigger_entry(e, trigger) for e in entries):
        wrapped = CommentedMap()
        wrapped["then"] = entries
        entries = [wrapped]
    apply_list_entry_upsert(entries, item, index, label=key)
    return strategy.resplice(yaml_text, key, entries)


def delete_list_entry_for(
    yaml_text: str,
    *,
    key: str,
    index: int,
    strategy: ListContainerStrategy,
) -> tuple[str, YamlDiff]:
    """Drop entry *index* from a ``<key>:`` list and re-emit the block."""
    container = strategy.locate(yaml_text, ErrorCode.NOT_FOUND)
    entries = container.get(key) if container is not None else None
    if not isinstance(entries, list) or not 0 <= index < len(entries):
        raise CommandError(ErrorCode.NOT_FOUND, strategy.not_present_msg(key, index))
    drop_after_block_comment(entries)
    del entries[index]
    return strategy.resplice(yaml_text, key, entries)


def _component_not_present_msg(component_id: str, key: str, index: int) -> str:
    """Delete not-found message for a component-nested list handler."""
    return f"{key}[{index}] not present on component id={component_id!r}"


def _component_strategy(domain: str, component_id: str) -> ListContainerStrategy:
    """Strategy for a list handler nested under a configured component instance."""
    return ListContainerStrategy(
        locate=partial(_require_instance, domain=domain, component_id=component_id),
        resplice=partial(_resplice_list_block, domain=domain, component_id=component_id),
        not_present_msg=partial(_component_not_present_msg, component_id),
    )


def upsert_component_on_entry(
    yaml_text: str,
    *,
    tree: AutomationTree,
    domain: str,
    component_id: str,
    trigger_key: str,
    trigger: AutomationTrigger,
    index: int,
) -> tuple[str, YamlDiff]:
    """Insert or replace one entry of a list-shaped trigger (``time.on_time``)."""
    return upsert_list_entry(
        yaml_text,
        key=trigger_key,
        item=emit_trigger_list_item(tree),
        index=index,
        strategy=_component_strategy(domain, component_id),
        trigger=trigger,
    )


def _require_instance(
    yaml_text: str, error_code: ErrorCode, *, domain: str, component_id: str
) -> dict:
    """
    Return the ``<domain>:`` list item whose ``id`` matches *component_id*.

    ``error_code`` selects the ``CommandError`` code so callers keep their
    INVALID_ARGS (upsert) / NOT_FOUND (delete) contracts.
    """
    data = make_yaml().load(yaml_text) or {}
    section = data.get(domain) if isinstance(data, dict) else None
    if isinstance(section, list):
        for instance in section:
            if isinstance(instance, dict) and str(instance.get(_ID_KEY, "")) == component_id:
                return instance
        # Fall back to the parser's positional ``<domain>_<idx>`` label for
        # an id-less instance (only when that instance is genuinely id-less).
        idx = synthetic_instance_index(domain, component_id)
        if idx is not None and idx < len(section):
            candidate = section[idx]
            if isinstance(candidate, dict) and _ID_KEY not in candidate:
                return candidate
    elif isinstance(section, dict):
        # Flat singleton block (``logger:`` / ``mqtt:`` / ``sun:``): the block
        # is the instance. Match its declared ``id``, else the domain name when
        # the ``id`` key is absent (key presence, not value, so ``id: null``
        # isn't treated as id-less; mirrors the list branch and the text-layer
        # ``_locate_singleton_instance``).
        if _ID_KEY in section:
            if str(section[_ID_KEY]) == component_id:
                return section
        elif component_id == domain:
            return section
    msg = f"Component instance id={component_id!r} not found under {domain!r}"
    raise CommandError(error_code, msg)


def delete_list_entry(
    yaml_text: str, *, domain: str, component_id: str, handler_key: str, index: int
) -> tuple[str, YamlDiff]:
    """Drop entry *index* from a component's ``<handler_key>:`` list; re-splice."""
    return delete_list_entry_for(
        yaml_text,
        key=handler_key,
        index=index,
        strategy=_component_strategy(domain, component_id),
    )


def upsert_light_effect(
    yaml_text: str,
    tree: AutomationTree,
    location: LightEffectLocation,
) -> tuple[str, YamlDiff]:
    """
    Splice an ``effects:`` list item under a configured light at ``location.index``.

    ``index == len(effects)`` appends; an in-range index replaces. Sibling
    effects are preserved — only the targeted entry changes.
    """
    # The tree carries the effect id under ``trigger_params`` (one
    # key mapping to its params dict). Reverse the parser's shape.
    if not tree.trigger_params or len(tree.trigger_params) != 1:
        msg = "LightEffect upsert requires exactly one effect-id key in trigger_params"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    effect_id, params = next(iter(tree.trigger_params.items()))
    catalog_entry = catalog.light_effect_by_id(str(effect_id))
    if catalog_entry is None:
        msg = f"Unknown light effect id: {effect_id!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    item = emit_effect_item(catalog_entry, str(effect_id), params or {})
    return upsert_list_entry(
        yaml_text,
        key="effects",
        item=item,
        index=location.index,
        strategy=_component_strategy("light", location.component_id),
    )


def delete_light_effect(
    yaml_text: str,
    location: LightEffectLocation,
) -> tuple[str, YamlDiff]:
    """Drop one entry from a light's ``effects:`` list."""
    return delete_list_entry(
        yaml_text,
        domain="light",
        component_id=location.component_id,
        handler_key="effects",
        index=location.index,
    )
