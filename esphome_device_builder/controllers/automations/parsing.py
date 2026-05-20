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

Unknown action / condition ids raise
``CommandError(INVALID_ARGS, ...)`` rather than best-effort
rebuilding — the frontend renders that as "edit raw YAML".
"""

from __future__ import annotations

from io import StringIO
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from ...helpers.api import CommandError
from ...models.api import ErrorCode
from ...models.automations import (
    ActionNode,
    ApiActionLocation,
    AutomationTree,
    ComponentOnLocation,
    ConditionNode,
    DeviceOnLocation,
    IntervalLocation,
    LightEffectLocation,
    ParsedAutomation,
    ScriptLocation,
)
from . import catalog

# Device-level trigger keys under the ``esphome:`` block.
_DEVICE_TRIGGER_KEYS: tuple[str, ...] = ("on_boot", "on_loop", "on_shutdown")


def make_yaml() -> YAML:
    """
    Build the round-trip YAML parser/emitter the controller shares.

    Two-space mapping indent matches ESPHome's canonical layout;
    ``preserve_quotes`` keeps quoted scalars like ``"on"`` intact so
    a quoted boolean-looking string round-trips unchanged.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


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
    out.extend(_parse_light_effects(data))
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


# Cache of component domains that host inline ``on_*:`` triggers,
# derived from the catalog on first use.
_COMPONENT_TRIGGER_DOMAINS: set[str] | None = None


def _component_trigger_domains() -> set[str]:
    """Return every top-level domain that hosts inline component triggers."""
    global _COMPONENT_TRIGGER_DOMAINS  # noqa: PLW0603 — module-level cache
    if _COMPONENT_TRIGGER_DOMAINS is not None:
        return _COMPONENT_TRIGGER_DOMAINS
    out: set[str] = set()
    for trigger in catalog.all_triggers():
        if trigger.is_device_level:
            continue
        out.update(trigger.applies_to)
    _COMPONENT_TRIGGER_DOMAINS = out
    return out


def _parse_device_level(root: Any) -> list[ParsedAutomation]:
    """Parse ``esphome.on_boot`` / ``on_loop`` / ``on_shutdown``."""
    esphome = root.get("esphome") if isinstance(root, dict) else None
    if not isinstance(esphome, dict):
        return []
    out: list[ParsedAutomation] = []
    for trigger_key in _DEVICE_TRIGGER_KEYS:
        if trigger_key not in esphome:
            continue
        body = esphome[trigger_key]
        from_line, to_line = _key_range(esphome, trigger_key)
        tree = _decompose_trigger_body(body, trigger_id=trigger_key)
        out.append(
            ParsedAutomation(
                location=DeviceOnLocation(trigger=trigger_key),
                label=_pretty_name(trigger_key),
                automation=tree,
                from_line=from_line,
                to_line=to_line,
                raw_yaml=_dump_slice({trigger_key: body}),
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
        tree = AutomationTree(
            trigger_id=None,
            trigger_params=_collect_block_params(item, action_list_keys={"then"}),
            actions=_decompose_action_list(item.get("then")),
        )
        out.append(
            ParsedAutomation(
                location=ScriptLocation(id=str(script_id)),
                label=f"Script: {script_id}",
                automation=tree,
                from_line=from_line,
                to_line=to_line,
                raw_yaml=_dump_slice([item]),
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
        tree = AutomationTree(
            trigger_id=None,
            trigger_params=_collect_block_params(item, action_list_keys={"then"}),
            actions=_decompose_action_list(item.get("then")),
        )
        out.append(
            ParsedAutomation(
                location=IntervalLocation(index=idx),
                label=label,
                automation=tree,
                from_line=from_line,
                to_line=to_line,
                raw_yaml=_dump_slice([item]),
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
        tree = AutomationTree(
            trigger_id=None,
            trigger_params=_collect_api_action_params(item),
            actions=_decompose_action_list(item.get("then")),
        )
        out.append(
            ParsedAutomation(
                location=ApiActionLocation(action_name=str(action_name)),
                label=f"API: {action_name}",
                automation=tree,
                from_line=from_line,
                to_line=to_line,
                raw_yaml=_dump_slice([item]),
            )
        )
    return out


def _parse_inline_component_triggers(root: Any) -> list[ParsedAutomation]:
    """Walk configured component instances for inline ``on_*:`` handlers."""
    if not isinstance(root, dict):
        return []
    out: list[ParsedAutomation] = []
    for domain, section in root.items():
        if domain not in _component_trigger_domains():
            continue
        if not isinstance(section, list):
            continue
        for idx, instance in enumerate(section):
            if not isinstance(instance, dict):
                continue
            comp_id = instance.get("id") or f"{domain}_{idx}"
            comp_name = instance.get("name") or comp_id
            for key, body in list(instance.items()):
                if not key.startswith("on_"):
                    continue
                trigger_id = f"{domain}.{key}"
                if catalog.trigger_by_id(trigger_id) is None:
                    # Not a known component trigger — skip rather
                    # than surface as a parse error. Component
                    # schemas occasionally carry ``on_*`` keys that
                    # are config values rather than automations
                    # (e.g. legacy aliases). The catalog is the
                    # source of truth.
                    continue
                from_line, to_line = _key_range(instance, key)
                tree = _decompose_trigger_body(body, trigger_id=trigger_id)
                out.append(
                    ParsedAutomation(
                        location=ComponentOnLocation(
                            component_id=str(comp_id),
                            trigger=key,
                        ),
                        label=f"{comp_name} → {_pretty_name(key)}",
                        automation=tree,
                        from_line=from_line,
                        to_line=to_line,
                        raw_yaml=_dump_slice({key: body}),
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


def _decompose_trigger_body(body: Any, *, trigger_id: str) -> AutomationTree:
    """
    Build an :class:`AutomationTree` from a trigger handler's body.

    Accepts three YAML shortcut forms that all collapse to the same
    tree: bare action list (``on_press: - action: ...``), single
    bare action (``on_press: action: ...``), explicit ``then:``.
    """
    trigger_params: dict[str, Any] = {}
    actions: list[ActionNode] = []

    if body is None:
        return AutomationTree(
            trigger_id=trigger_id,
            trigger_params={},
            actions=[],
        )

    if isinstance(body, list):
        actions = _decompose_action_list(body)
    elif isinstance(body, dict):
        trigger_params = _collect_block_params(body, action_list_keys={"then"})
        if "then" in body:
            actions = _decompose_action_list(body["then"])
        else:
            # Single-action shortcut: the body's keys are a mix of
            # trigger params and known catalog action ids.
            # ``_collect_block_params`` naively absorbed both; pull
            # the action keys back out by catalog lookup and rebuild
            # ``trigger_params`` without them.
            action_body = {k: v for k, v in body.items() if catalog.action_by_id(k) is not None}
            if action_body:
                actions = _decompose_action_list([action_body])
                trigger_params = {k: v for k, v in trigger_params.items() if k not in action_body}

    return AutomationTree(
        trigger_id=trigger_id,
        trigger_params=trigger_params,
        actions=actions,
    )


def _decompose_action_list(body: Any) -> list[ActionNode]:
    """
    Recursively turn a YAML action-list body into a list of nodes.

    Accepts a list of action mappings, a single mapping, or ``None``.
    Each mapping is the registry-shape ``{<action_id>: <params>}``.
    """
    if body is None:
        return []
    items = body if isinstance(body, list) else [body]
    out: list[ActionNode] = []
    for item in items:
        if not isinstance(item, dict) or not item:
            continue
        for action_id, params in item.items():
            out.append(_decompose_action(str(action_id), params))
    return out


def _decompose_action(action_id: str, raw_params: Any) -> ActionNode:
    """Build one :class:`ActionNode` from a registry-shaped mapping entry."""
    action = catalog.action_by_id(action_id)
    if action is None:
        msg = f"Unknown action id: {action_id!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    children: dict[str, list[ActionNode]] = {}
    conditions: list[ConditionNode] = []

    if raw_params is None:
        params: dict[str, Any] = {}
    elif isinstance(raw_params, dict):
        params = {}
        for key, value in raw_params.items():
            if key in action.accepts_action_list:
                children[key] = _decompose_action_list(value)
                continue
            if key in ("condition", "all", "any"):
                conditions = _decompose_condition_list(value)
                continue
            params[key] = _render_value(value)
    else:
        # Bare-id shortcut (e.g. ``light.turn_on: living_room``):
        # surface the scalar under the ``id`` key so the writer can
        # reconstruct the short form on round-trip.
        params = {"id": _render_value(raw_params)}

    return ActionNode(
        action_id=action_id,
        params=params,
        children=children,
        conditions=conditions,
    )


def _decompose_condition_list(body: Any) -> list[ConditionNode]:
    """Turn a ``condition`` / ``and`` / ``or`` / ``not`` body into nodes."""
    if body is None:
        return []
    if isinstance(body, list):
        return [_decompose_condition(item) for item in body if isinstance(item, dict)]
    if isinstance(body, dict):
        return [_decompose_condition(body)]
    return []


def _decompose_condition(raw: dict) -> ConditionNode:
    """Build one :class:`ConditionNode` from a registry-shaped entry."""
    if not raw or not isinstance(raw, dict):
        msg = "Empty condition entry"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if len(raw) != 1:
        msg = f"Condition entry must carry a single id key, got: {sorted(raw)}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    cond_id, value = next(iter(raw.items()))
    catalog_entry = catalog.condition_by_id(str(cond_id))
    if catalog_entry is None:
        msg = f"Unknown condition id: {cond_id!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    children: list[ConditionNode] = []
    params: dict[str, Any] = {}
    if catalog_entry.accepts_condition_list:
        children = _decompose_condition_list(value)
    elif isinstance(value, dict):
        params = {k: _render_value(v) for k, v in value.items()}
    elif value is not None:
        params = {"id": _render_value(value)}
    return ConditionNode(
        condition_id=str(cond_id),
        params=params,
        children=children,
    )


def _collect_block_params(
    block: dict,
    *,
    action_list_keys: set[str],
) -> dict[str, Any]:
    """Collect non-action-list keys as plain ``params`` values."""
    out: dict[str, Any] = {}
    for key, value in block.items():
        if key in action_list_keys:
            continue
        out[key] = _render_value(value)
    return out


def _collect_api_action_params(block: dict) -> dict[str, Any]:
    """Collect ``api.actions:`` item params, dropping the discriminator + ``then:``."""
    out: dict[str, Any] = {}
    for key, value in block.items():
        if key in ("then", "action", "service"):
            continue
        out[key] = _render_value(value)
    return out


def _render_value(value: Any) -> Any:
    """
    Convert a ruamel-parsed value to its JSON-wire shape.

    Lambda block scalars (``|`` or ``!lambda`` tagged) become the
    ``{"_lambda": "<source>"}`` sentinel; ruamel maps and lists
    become plain dicts/lists, recursively.
    """
    if isinstance(value, LiteralScalarString):
        return {"_lambda": str(value)}
    # Tagged ``!lambda`` scalars come through ruamel as a regular
    # string carrying a ``.yaml_tag`` attribute; the LiteralScalar
    # branch above handles the common case of an unmarked ``|``
    # block, which ESPHome treats as a lambda when the schema's
    # ``templatable`` flag is set.
    tag = getattr(value, "yaml_tag", None)
    if tag and getattr(tag, "value", "") == "!lambda":
        return {"_lambda": str(value)}
    if isinstance(value, dict):
        return {k: _render_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_value(v) for v in value]
    return value


def _render_params(value: Any) -> Any:
    """Wrap an arbitrary ruamel value as a plain dict for ``params``."""
    rendered = _render_value(value)
    if isinstance(rendered, dict):
        return rendered
    return {"_value": rendered}


def _pretty_name(key: str) -> str:
    """Title-case an ``on_x_y`` key for display labels."""
    return key.replace("_", " ").title()


def _key_range(mapping: Any, key: str) -> tuple[int, int]:
    """Return the 1-indexed line range covering ``mapping[key]``."""
    lc = getattr(mapping, "lc", None)
    if lc is None or not getattr(lc, "data", None) or key not in lc.data:
        return 1, 1
    key_line, _key_col, _val_line, _val_col = lc.data[key]
    start = key_line + 1
    end = _estimate_end_line(mapping[key], start)
    return start, end


def _item_range(seq: Any, idx: int) -> tuple[int, int]:
    """Return the 1-indexed line range for the *idx*'th list item."""
    lc = getattr(seq, "lc", None)
    if lc is None or not getattr(lc, "data", None) or idx not in lc.data:
        return 1, 1
    # Use the dash-line index so leading blank / comment lines
    # don't shift the start.
    dash_line = lc.data[idx][0]
    start = dash_line + 1
    end = _estimate_end_line(seq[idx], start)
    return start, end


def _estimate_end_line(value: Any, start: int) -> int:
    """Walk a sub-tree and pick the largest ``lc.line`` we observe."""
    max_line = start
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        lc = getattr(node, "lc", None)
        if lc is not None and getattr(lc, "line", None) is not None:
            max_line = max(max_line, lc.line + 1)
        if isinstance(node, dict):
            stack.extend(node.values())
            data = getattr(lc, "data", None) if lc else None
            if data:
                for entry in data.values():
                    # ruamel entries are (key_line, key_col, val_line, val_col)
                    if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                        max_line = max(max_line, entry[2] + 1)
        elif isinstance(node, list):
            stack.extend(node)
            # ruamel sequence ``lc.data`` entries are 2-tuples
            # (dash_line, dash_col) — they don't carry a value-line
            # we could use, so we rely on the recursive walk into
            # the inner mapping for the actual end line.
    return max_line


def _dump_slice(value: Any) -> str:
    """Serialise *value* through the round-trip emitter as a YAML string."""
    yaml = make_yaml()
    buf = StringIO()
    yaml.dump(value, buf)
    return buf.getvalue()
