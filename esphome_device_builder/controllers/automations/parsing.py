"""
YAML → :class:`ParsedAutomation` list.

ruamel.yaml round-trip mode preserves the user's comments, key
order, blank lines, and quoting so a "no-op" round-trip through
parse → upsert leaves the document visually identical. The parser
walks five shapes:

- Top-level ``script:`` and ``interval:`` list blocks.
- ``esphome.on_boot`` / ``on_loop`` / ``on_shutdown``.
- ``api.actions:`` user-defined callable list items.
- Configured component instances with inline ``on_*:`` handlers.
- Light ``effects:`` lists.

An unknown action / condition id fails only the automation that
carries it: that entry comes back with ``error`` set and an empty
tree (the frontend renders it read-only as "edit raw YAML"), while
every sibling parses normally. A YAML that won't load at all is the
one whole-document failure that still raises.

Body decomposition (handler body → tree) lives in :mod:`._decompose`;
source-line mapping in :mod:`._ranges`; the shared YAML factory in
:mod:`._yaml`. This module owns the document walk that stitches them
into the ordered :class:`ParsedAutomation` list.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from importlib import resources
from typing import Any, NamedTuple

from ...helpers.api import CommandError
from ...helpers.json import loads as json_loads
from ...helpers.lazy_catalog import is_unsafe_catalog_id
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
    ParsedAutomation,
    ScriptLocation,
)
from . import catalog
from ._decompose import (
    DEFAULT_SHORTHAND_KEY,
    _block_tree,
    _collect_api_action_params,
    _collect_block_params,
    _decompose_action,
    _decompose_action_list,
    _decompose_condition,
    _decompose_condition_list,
    _decompose_trigger_body,
    _decompose_trigger_mapping,
    _render_params,
    _render_value,
    _safe_tree,
)
from ._ranges import _dump_slice, _estimate_end_line, _item_range, _key_range, _pretty_name
from ._yaml import make_yaml

__all__ = [
    "DEFAULT_SHORTHAND_KEY",
    "_decompose_action",
    "_decompose_action_list",
    "_decompose_condition",
    "_decompose_condition_list",
    "_decompose_trigger_mapping",
    "_dump_slice",
    "_estimate_end_line",
    "_item_range",
    "_key_range",
    "_render_params",
    "_render_value",
    "catalog_id",
    "iter_subentities",
    "make_yaml",
    "parse_device_yaml",
    "platform_subentity_keys",
    "resolve_component_domain",
    "resolve_component_target",
    "singleton_component_id",
]

# Package holding the per-component body JSON (``config_entries`` trees).
_COMPONENTS_PACKAGE = "esphome_device_builder.definitions.components"


def parse_device_yaml(yaml_text: str) -> list[ParsedAutomation]:
    """
    Walk *yaml_text* and return every automation we recognise.

    Output mirrors document order: device-level → scripts → intervals →
    inline component handlers → light effects. ``from_line`` /
    ``to_line`` are 1-indexed against the input YAML so the navigator
    can map a click to the right range without re-parsing.
    """
    yaml = make_yaml()
    try:
        data = yaml.load(yaml_text)
    except Exception as err:
        msg = f"Failed to parse device YAML: {err}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg) from err
    if data is None:
        return []

    out: list[ParsedAutomation] = []
    out.extend(_parse_device_level(data))
    out.extend(_parse_top_level_scripts(data))
    out.extend(_parse_top_level_intervals(data))
    out.extend(_parse_api_actions(data))
    out.extend(_parse_inline_component_triggers(data))
    out.extend(_parse_component_action_fields(data))
    out.extend(_parse_light_effects(data))
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _parse_device_level(root: Any) -> list[ParsedAutomation]:
    """Parse device-level ``esphome.on_*`` handlers (mapping or list form).

    The device-trigger set is catalog-derived (``is_device_level``), not a hand
    list, so a new device trigger flows in on the next sync. A list of handler
    entries (multiple ``on_boot`` priorities) decomposes per entry; a mapping or
    bare action list stays one automation.
    """
    esphome = root.get("esphome") if isinstance(root, dict) else None
    if not isinstance(esphome, dict):
        return []
    out: list[ParsedAutomation] = []
    for trigger_key in catalog.device_trigger_ids():
        if trigger_key not in esphome:
            continue
        body = esphome[trigger_key]
        trigger = catalog.trigger_by_id(trigger_key)
        if trigger is not None and _is_list_form_trigger(body, trigger):
            out.extend(
                _parse_trigger_list(
                    body,
                    trigger_id=trigger_key,
                    location_for=partial(DeviceOnLocation, trigger_key),
                    label_prefix=_pretty_name(trigger_key),
                )
            )
            continue
        from_line, to_line = _key_range(esphome, trigger_key)
        tree, error = _safe_tree(
            partial(_decompose_trigger_body, body, trigger_id=trigger_key),
            trigger_id=trigger_key,
        )
        out.append(
            ParsedAutomation(
                location=DeviceOnLocation(trigger=trigger_key),
                label=_pretty_name(trigger_key),
                automation=tree,
                from_line=from_line,
                to_line=to_line,
                raw_yaml=_dump_slice({trigger_key: body}),
                error=error,
            )
        )
    return out


def _parse_top_level_scripts(root: Any) -> list[ParsedAutomation]:
    """Parse top-level ``script:`` list blocks."""
    if not isinstance(root, dict):
        return []
    scripts = root.get("script")
    if not isinstance(scripts, list):
        return []
    out: list[ParsedAutomation] = []
    for idx, item in enumerate(scripts):
        if not isinstance(item, dict):
            continue
        script_id = item.get("id") or f"script_{idx}"
        from_line, to_line = _item_range(scripts, idx)
        tree, error = _safe_tree(
            partial(
                _block_tree,
                _collect_block_params(item, action_list_keys={"then"}),
                item.get("then"),
            ),
            trigger_id=None,
        )
        out.append(
            ParsedAutomation(
                location=ScriptLocation(id=str(script_id)),
                label=f"Script: {script_id}",
                automation=tree,
                from_line=from_line,
                to_line=to_line,
                raw_yaml=_dump_slice([item]),
                error=error,
            )
        )
    return out


def _parse_top_level_intervals(root: Any) -> list[ParsedAutomation]:
    """Parse top-level ``interval:`` list blocks."""
    if not isinstance(root, dict):
        return []
    intervals = root.get("interval")
    if not isinstance(intervals, list):
        return []
    out: list[ParsedAutomation] = []
    for idx, item in enumerate(intervals):
        if not isinstance(item, dict):
            continue
        from_line, to_line = _item_range(intervals, idx)
        every = item.get("interval")
        label = f"Interval: every {every}" if every else f"Interval #{idx + 1}"
        tree, error = _safe_tree(
            partial(
                _block_tree,
                _collect_block_params(item, action_list_keys={"then"}),
                item.get("then"),
            ),
            trigger_id=None,
        )
        out.append(
            ParsedAutomation(
                location=IntervalLocation(index=idx),
                label=label,
                automation=tree,
                from_line=from_line,
                to_line=to_line,
                raw_yaml=_dump_slice([item]),
                error=error,
            )
        )
    return out


def _parse_api_actions(root: Any) -> list[ParsedAutomation]:
    """
    Parse ``api.actions:`` list items as callable automations.

    Structurally a near-duplicate of ``script:`` — named callable
    with typed ``variables:`` and a ``then:`` action list — so the
    same :class:`AutomationTree` shape carries it. The deprecated
    ``service:`` discriminator key is accepted as an alias for
    ``action:`` on read; the writer emits ``action:``.
    """
    if not isinstance(root, dict):
        return []
    api_block = root.get("api")
    if not isinstance(api_block, dict):
        return []
    actions = api_block.get("actions")
    if not isinstance(actions, list):
        return []
    out: list[ParsedAutomation] = []
    for idx, item in enumerate(actions):
        if not isinstance(item, dict):
            continue
        action_name = item.get("action") or item.get("service")
        if not action_name:
            continue
        from_line, to_line = _item_range(actions, idx)
        tree, error = _safe_tree(
            partial(_block_tree, _collect_api_action_params(item), item.get("then")),
            trigger_id=None,
        )
        out.append(
            ParsedAutomation(
                location=ApiActionLocation(action_name=str(action_name)),
                label=f"API: {action_name}",
                automation=tree,
                from_line=from_line,
                to_line=to_line,
                raw_yaml=_dump_slice([item]),
                error=error,
            )
        )
    return out


def singleton_component_id(section: dict, domain: str) -> str:
    """Identity of a flat singleton component — its ``id:`` or the domain when id-less."""
    return str(section.get("id") or domain)


class ComponentTarget(NamedTuple):
    """
    Where a *component_id* resolves in the YAML.

    A top-level instance, or a nested sub-entity carrying the parent context
    (``parent_domain`` / ``parent_id`` / ``sub_key``) the writer splices under.
    """

    domain: str
    is_sub_entity: bool = False
    parent_domain: str | None = None
    parent_id: str | None = None
    sub_key: str | None = None


def resolve_component_domain(yaml_text: str, component_id: str) -> str | None:
    """
    Return the top-level domain whose instance declares *component_id*.

    Thin wrapper over :func:`resolve_component_target` returning the
    top-level domain (the parent's for a sub-entity). ``None`` when no
    instance matches or the YAML won't load.
    """
    target = resolve_component_target(yaml_text, component_id)
    if target is None:
        return None
    return target.parent_domain or target.domain


def resolve_component_target(yaml_text: str, component_id: str) -> ComponentTarget | None:
    """
    Resolve *component_id* to a top-level instance or a nested sub-entity.

    Walks declared instance ids — list instances on ``id`` (or the synthetic
    ``<domain>_<idx>``), flat singletons on :func:`singleton_component_id` —
    and each instance's catalog sub-entity blocks. Only declared ids match;
    an action *reference* nested in a handler body never does. ``None`` when
    no instance matches or the YAML won't load.
    """
    yaml = make_yaml()
    try:
        root = yaml.load(yaml_text)
    except Exception:  # noqa: BLE001 — any load failure falls back to the catalog guess
        return None
    for _domain, _instance, comp_id, target in _iter_instance_targets(root):
        if comp_id == component_id:
            return target
    return None


def _instance_id(domain: str, instance: dict, idx: int, *, is_list: bool) -> str:
    """Reconstruct the id the parser attributes to one instance."""
    if is_list:
        return str(instance.get("id") or f"{domain}_{idx}")
    return singleton_component_id(instance, domain)


def _iter_instance_targets(
    root: Any,
) -> Iterator[tuple[str, dict, str, ComponentTarget]]:
    """
    Yield ``(domain, instance, comp_id, target)`` for every instance + sub-entity.

    The one document walk shared by :func:`_iter_component_instances` and
    :func:`resolve_component_target` (list instances, flat singletons, nested
    sub-entities).
    """
    if not isinstance(root, dict):
        return
    for domain, section in root.items():
        is_list = isinstance(section, list)
        items = section if is_list else [section] if isinstance(section, dict) else []
        for idx, instance in enumerate(items):
            if not isinstance(instance, dict):
                continue
            comp_id = _instance_id(str(domain), instance, idx, is_list=is_list)
            yield str(domain), instance, comp_id, ComponentTarget(domain=str(domain))
            for sub_domain, sub, sub_id, sub_key in iter_subentities(str(domain), instance):
                yield (
                    sub_domain,
                    sub,
                    sub_id,
                    ComponentTarget(
                        domain=sub_domain,
                        is_sub_entity=True,
                        parent_domain=str(domain),
                        parent_id=comp_id,
                        sub_key=sub_key,
                    ),
                )


def _iter_component_instances(
    root: Any,
) -> Iterator[tuple[str, dict, str]]:
    """Yield ``(domain, instance, comp_id)`` for every instance + sub-entity."""
    for domain, instance, comp_id, _target in _iter_instance_targets(root):
        yield domain, instance, comp_id


def catalog_id(domain: str, platform: Any) -> str:
    """Return the component's catalog id: ``<domain>.<platform>``, or the bare domain."""
    return f"{domain}.{platform}" if isinstance(platform, str) and platform else domain


def iter_subentities(
    domain: str,
    instance: dict,
) -> Iterator[tuple[str, dict, str, str]]:
    """Yield ``(platform_type, sub_instance, sub_id, sub_key)`` for ided sub-blocks."""
    cat_id = catalog_id(domain, instance.get("platform"))
    for sub_key, sub_domain in platform_subentity_keys(cat_id):
        sub = instance.get(sub_key)
        if isinstance(sub, dict) and sub.get("id") is not None:
            yield sub_domain, sub, str(sub["id"]), sub_key


def _parse_inline_component_triggers(root: Any) -> list[ParsedAutomation]:
    """Walk component instances for inline ``on_*:`` handlers."""
    trigger_domains = catalog.component_trigger_domains()
    out: list[ParsedAutomation] = []
    for domain, instance, comp_id in _iter_component_instances(root):
        if domain not in trigger_domains:
            continue
        out.extend(_parse_instance_triggers(domain, instance, comp_id))
    return out


def _component_body_entries(catalog_id: str) -> list[Any]:
    """
    Return *catalog_id*'s top-level ``config_entries`` list (``[]`` when absent).

    Reads the shipped component body JSON (the same the components controller
    serves). Only ``FileNotFoundError`` is swallowed — the expected "no shipped
    catalog entry" case; a real read error or a missing definitions package
    (``ModuleNotFoundError``, a packaging defect that would otherwise silently
    disable the feature process-wide) propagates instead.
    """
    if is_unsafe_catalog_id(catalog_id):
        return []
    try:
        raw = resources.files(_COMPONENTS_PACKAGE).joinpath(f"{catalog_id}.json").read_bytes()
    except FileNotFoundError:
        return []
    if not raw:  # pragma: no cover - shipped catalog bodies are never empty
        return []
    return json_loads(raw).get("config_entries") or []


# Per-``<domain>.<platform>`` set of ``type: trigger`` action-list field
# keys, read from the component bodies on first use (process cache).
_ACTION_FIELD_INDEX: dict[str, frozenset[str]] = {}


def _component_action_fields(catalog_id: str) -> frozenset[str]:
    """Return the ``type: trigger`` action-list field keys for *catalog_id*."""
    cached = _ACTION_FIELD_INDEX.get(catalog_id)
    if cached is None:
        cached = frozenset(
            entry["key"]
            for entry in _component_body_entries(catalog_id)
            if isinstance(entry, dict)
            and entry.get("type") == "trigger"
            and isinstance(entry.get("key"), str)
        )
        _ACTION_FIELD_INDEX[catalog_id] = cached
    return cached


# Per-``<domain>.<platform>`` tuple of ``(sub_key, platform_type)`` pairs for
# the component's nested sub-entity blocks, read once per id (process cache).
_PLATFORM_SUBENTITY_INDEX: dict[str, tuple[tuple[str, str], ...]] = {}


def platform_subentity_keys(catalog_id: str) -> tuple[tuple[str, str], ...]:
    """
    Return ``(sub_key, platform_type)`` for *catalog_id*'s sub-entity blocks.

    A sub-entity block is a ``type: nested`` entry with a ``platform_type`` and
    its own ``id`` (``temperature`` on ``sensor.aht10``); plain groups
    (``availability:``) are excluded.
    """
    cached = _PLATFORM_SUBENTITY_INDEX.get(catalog_id)
    if cached is None:
        cached = tuple(
            (entry["key"], entry["platform_type"])
            for entry in _component_body_entries(catalog_id)
            if _is_subentity_block(entry)
        )
        _PLATFORM_SUBENTITY_INDEX[catalog_id] = cached
    return cached


def _is_subentity_block(entry: Any) -> bool:
    """Return True for a nested config entry that is itself an id'd platform sub-entity."""
    if not isinstance(entry, dict) or entry.get("type") != "nested":
        return False
    platform_type = entry.get("platform_type")
    return (
        isinstance(entry.get("key"), str)
        and isinstance(platform_type, str)
        and bool(platform_type)
        and any(
            isinstance(sub, dict) and sub.get("key") == "id"
            for sub in entry.get("config_entries") or []
        )
    )


def _parse_component_action_fields(root: Any) -> list[ParsedAutomation]:
    """
    Emit each ``type: trigger`` action-list config field on a component.

    Cover ``open_action`` / ``close_action`` / ``stop_action``, climate
    ``*_action``, … are bare action lists keyed on the field name — a
    trigger-less automation parallel to ``script:`` / ``api.actions:``.
    Reuses the shared instance walk and the same body decomposition as
    inline ``on_*`` handlers (``trigger_id`` is ``None`` — no trigger).
    """
    out: list[ParsedAutomation] = []
    for domain, instance, comp_id, target in _iter_instance_targets(root):
        # Action-list fields (``open_action`` …) are a top-level-component
        # concern; the shared walk also yields sub-entities (for inline
        # ``on_*`` parsing), so skip them here to keep the scope unchanged.
        if target.is_sub_entity:
            continue
        fields = _component_action_fields(catalog_id(domain, instance.get("platform")))
        if not fields:
            continue
        comp_name = str(instance.get("name") or comp_id)
        for key, body in list(instance.items()):
            if key not in fields:
                continue
            from_line, to_line = _key_range(instance, key)
            tree, error = _safe_tree(
                partial(_decompose_trigger_body, body, trigger_id=None),
                trigger_id=None,
            )
            out.append(
                ParsedAutomation(
                    location=ComponentActionFieldLocation(component_id=comp_id, field=key),
                    label=f"{comp_name} → {_pretty_name(key)}",
                    automation=tree,
                    from_line=from_line,
                    to_line=to_line,
                    raw_yaml=_dump_slice({key: body}),
                    error=error,
                )
            )
    return out


def _parse_instance_triggers(
    domain: str,
    instance: dict,
    comp_id: str,
) -> list[ParsedAutomation]:
    """Emit every recognised inline ``on_*:`` handler on one component instance."""
    comp_name = str(instance.get("name") or comp_id)
    out: list[ParsedAutomation] = []
    for key, body in list(instance.items()):
        if not key.startswith("on_"):
            continue
        trigger = catalog.trigger_by_id(f"{domain}.{key}")
        if trigger is None:
            # Not a known component trigger — skip rather than surface
            # as a parse error. Component schemas occasionally carry
            # ``on_*`` keys that are config values rather than
            # automations (e.g. legacy aliases). The catalog is the
            # source of truth.
            continue
        out.extend(
            _parse_one_inline_trigger(
                instance,
                comp_id=comp_id,
                comp_name=comp_name,
                key=key,
                body=body,
                trigger=trigger,
            )
        )
    return out


def _parse_one_inline_trigger(
    instance: dict,
    *,
    comp_id: str,
    comp_name: str,
    key: str,
    body: Any,
    trigger: AutomationTrigger,
) -> list[ParsedAutomation]:
    """Parse one inline ``on_*:`` handler — single mapping or list-shaped."""
    trigger_id = trigger.id
    if _is_list_form_trigger(body, trigger):
        return _parse_trigger_list(
            body,
            trigger_id=trigger_id,
            location_for=partial(ComponentOnLocation, comp_id, key),
            label_prefix=f"{comp_name} → {_pretty_name(key)}",
        )
    from_line, to_line = _key_range(instance, key)
    tree, error = _safe_tree(
        partial(_decompose_trigger_body, body, trigger_id=trigger_id),
        trigger_id=trigger_id,
    )
    return [
        ParsedAutomation(
            location=ComponentOnLocation(component_id=comp_id, trigger=key),
            label=f"{comp_name} → {_pretty_name(key)}",
            automation=tree,
            from_line=from_line,
            to_line=to_line,
            raw_yaml=_dump_slice({key: body}),
            error=error,
        )
    ]


def _is_list_form_trigger(body: Any, trigger: AutomationTrigger) -> bool:
    """
    Report whether *body* is a YAML list of trigger entries (``time.on_time``).

    A bare action list (``on_press: [{light.turn_on: id}, ...]``) is *not*
    list-form: every item there is a known action id. List-form requires a
    non-empty list whose every item is trigger-shaped (see
    :func:`_is_trigger_entry`), so the conservative default keeps the
    existing bare-action-list behaviour intact.
    """
    return (
        isinstance(body, list)
        and bool(body)
        and all(_is_trigger_entry(item, trigger) for item in body)
    )


def _is_trigger_entry(item: Any, trigger: AutomationTrigger) -> bool:
    """
    Report whether *item* looks like one entry of a list-shaped trigger.

    Requires a ``then:`` or one of the trigger's own config keys — a bare
    action item (including an unknown action id) is *not* an entry, so it
    stays a bare action list and its parse error still surfaces.
    """
    if not isinstance(item, dict) or not item:
        return False
    if "then" in item:
        return True
    cron_keys = {entry.key for entry in trigger.config_entries}
    return any(key in cron_keys for key in item)


def _parse_trigger_list(
    body: list,
    *,
    trigger_id: str | None,
    location_for: Callable[[int], AutomationLocation],
    label_prefix: str,
) -> list[ParsedAutomation]:
    """Emit one :class:`ParsedAutomation` per entry of a list-shaped handler.

    Shared by the device-level (``esphome.on_boot``) and inline-component
    (``time.on_time``) paths; *location_for* maps an index to the entry's
    location (a :func:`functools.partial` of the location class), *label_prefix*
    heads the per-entry ``"<prefix> #<n>"`` label.
    """
    out: list[ParsedAutomation] = []
    for index, entry in enumerate(body):
        from_line, to_line = _item_range(body, index)
        tree, error = _safe_tree(
            partial(_decompose_trigger_mapping, entry, trigger_id=trigger_id),
            trigger_id=trigger_id,
        )
        out.append(
            ParsedAutomation(
                location=location_for(index),
                label=f"{label_prefix} #{index + 1}",
                automation=tree,
                from_line=from_line,
                to_line=to_line,
                raw_yaml=_dump_slice([entry]),
                error=error,
            )
        )
    return out


def _parse_light_effects(root: Any) -> list[ParsedAutomation]:
    """Walk configured light instances for user-authored ``effects:`` items."""
    if not isinstance(root, dict):
        return []
    lights = root.get("light")
    if not isinstance(lights, list):
        return []
    out: list[ParsedAutomation] = []
    for inst_idx, instance in enumerate(lights):
        if not isinstance(instance, dict):
            continue
        comp_id = instance.get("id") or f"light_{inst_idx}"
        effects = instance.get("effects")
        if not isinstance(effects, list):
            continue
        for idx, item in enumerate(effects):
            if not isinstance(item, dict) or len(item) != 1:
                continue
            effect_id = next(iter(item))
            params = item[effect_id] or {}
            label = (
                f"{comp_id} → Effect: {params.get('name') or effect_id}"
                if isinstance(params, dict)
                else f"{comp_id} → Effect: {effect_id}"
            )
            from_line, to_line = _item_range(effects, idx)
            tree = AutomationTree(
                trigger_id=None,
                trigger_params={effect_id: _render_params(params)} if params else {effect_id: {}},
                actions=[],
            )
            out.append(
                ParsedAutomation(
                    location=LightEffectLocation(component_id=str(comp_id), index=idx),
                    label=label,
                    automation=tree,
                    from_line=from_line,
                    to_line=to_line,
                    raw_yaml=_dump_slice([item]),
                )
            )
    return out
