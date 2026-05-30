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

from ...helpers.api import CommandError
from ...helpers.yaml import remove_inline_handler, upsert_inline_handler
from ...models.api import ErrorCode
from ...models.automations import (
    AutomationTree,
    LightEffectLocation,
    YamlDiff,
)
from . import catalog
from .emitter import dump, emit_effect_item
from .parsing import make_yaml


def wrap_handler_list_block(handler_key: str, rendered_list: str) -> str:
    """Prefix a rendered dashed list with its ``<handler_key>:`` header."""
    # ``upsert_inline_handler`` writes ``rendered_yaml`` verbatim under the
    # component instance; the list dump is bare, so add the header here.
    return f"{handler_key}:\n" + rendered_list.rstrip() + "\n"


def upsert_light_effect(
    yaml_text: str,
    tree: AutomationTree,
    location: LightEffectLocation,
) -> tuple[str, YamlDiff]:
    """Splice an ``effects:`` list item under a configured light."""
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
    rendered = wrap_handler_list_block(
        "effects",
        dump([emit_effect_item(catalog_entry, str(effect_id), params or {})]),
    )
    res = upsert_inline_handler(
        yaml_text,
        component_domain="light",
        component_id=location.component_id,
        handler_key="effects",
        rendered_yaml=rendered,
    )
    if res is None:
        msg = f"Light instance id={location.component_id!r} not found; can't splice effect entry"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    new_text, from_line, to_line, replacement = res
    return new_text, YamlDiff(
        fromLine=from_line,
        toLine=to_line,
        replacement=replacement,
    )


def delete_light_effect(
    yaml_text: str,
    location: LightEffectLocation,
) -> tuple[str, YamlDiff]:
    """Drop one entry from a light's ``effects:`` list."""
    # Easiest path: parse, mutate the list, re-emit. We don't have a
    # line-precise splice helper for "remove list item at index N
    # inside an inline handler" — this keeps the writer simple at
    # the cost of touching the whole ``effects:`` block in the diff.
    yaml = make_yaml()
    data = yaml.load(yaml_text) or {}
    lights = data.get("light") if isinstance(data, dict) else None
    if not isinstance(lights, list):
        msg = "No light: block; can't delete effect"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    for instance in lights:
        if not isinstance(instance, dict):
            continue
        if str(instance.get("id", "")) != location.component_id:
            continue
        effects = instance.get("effects")
        if not isinstance(effects, list) or not 0 <= location.index < len(effects):
            msg = f"effects[{location.index}] not present on light id={location.component_id!r}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        del effects[location.index]
        if not effects:
            del instance["effects"]
        # Re-render the inline handler block to splice through
        # ``upsert_inline_handler`` (or remove it when empty).
        if "effects" in instance:
            rendered = wrap_handler_list_block("effects", dump(effects))
            res = upsert_inline_handler(
                yaml_text,
                component_domain="light",
                component_id=location.component_id,
                handler_key="effects",
                rendered_yaml=rendered,
            )
            if res is None:  # pragma: no cover — instance found above
                msg = f"light id={location.component_id!r} not found in splice"
                raise CommandError(ErrorCode.INTERNAL_ERROR, msg)
            new_text, from_line, to_line, replacement = res
            return new_text, YamlDiff(
                fromLine=from_line,
                toLine=to_line,
                replacement=replacement,
            )
        removed = remove_inline_handler(
            yaml_text,
            component_domain="light",
            component_id=location.component_id,
            handler_key="effects",
        )
        if removed is None:  # pragma: no cover — instance found above
            msg = f"effects: not found on light id={location.component_id!r}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)
        new_text, from_line, to_line = removed
        return new_text, YamlDiff(fromLine=from_line, toLine=to_line, replacement="")
    msg = f"Light id={location.component_id!r} not found"
    raise CommandError(ErrorCode.NOT_FOUND, msg)
