"""YAML → :class:`ParsedAutomation` list.

ruamel.yaml round-trip mode preserves the user's comments, key
order, blank lines, and quoting so a "no-op" round-trip through
parse → upsert leaves the document visually identical. The parser
walks four shapes:

- Top-level ``script:`` and ``interval:`` list blocks → one
  :class:`ParsedAutomation` per list item.
- ``esphome:`` block's ``on_boot`` / ``on_loop`` / ``on_shutdown``
  → device-level automations.
- Configured component instances with inline ``on_*:`` handlers
  (binary_sensor's ``on_press``, light's ``on_turn_on``, ...) →
  one ``ParsedAutomation`` per inline handler.
- A light component's ``effects:`` list → one
  :class:`ParsedAutomation` per user-authored effect entry.

Two trigger shortcut forms parse identically and canonicalise to
the explicit ``then:`` form on the way back out:

* ``on_press: - light.turn_on: id`` (bare action list)
* ``on_press: light.turn_on: id`` (single action shortcut)

Unknown action / condition ids raise
``CommandError(INVALID_ARGS, ...)`` rather than best-effort
rebuilding. The frontend renders that as a "this automation
references a non-catalog action — edit raw YAML" hint.
"""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from ...helpers.api import CommandError
from ...models.api import ErrorCode
from ...models.automations import (
    ActionNode,
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

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def make_yaml() -> YAML:
    """Build the round-trip YAML parser/emitter the controller shares.

    Indent matches ESPHome's canonical two-space layout. The
    ``preserve_quotes`` flag keeps quoted scalars quoted so a YAML
    that uses ``"on"`` to disable boolean coercion round-trips
    unchanged. ``width`` is set high so long action lists don't get
    folded across lines.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml


# Component domains whose entries can carry inline ``on_*:`` handlers.
# Read from the catalog at first call so the set stays in sync with
# whatever the schema bundle declares.
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


# Device-level trigger keys under the ``esphome:`` block.
_DEVICE_TRIGGER_KEYS: tuple[str, ...] = ("on_boot", "on_loop", "on_shutdown")


def parse_device_yaml(yaml_text: str) -> list[ParsedAutomation]:
    """
    Walk *yaml_text* and return every automation we recognise.

    The list mirrors document order — top-level blocks first
    (``script:`` / ``interval:`` / ``esphome.on_*``), then inline
    component handlers, then light effects. ``from_line`` /
    ``to_line`` line numbers are 1-indexed from ruamel's ``lc``
    attribute so the navigator can map a click back to the YAML
    pane.
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
    out.extend(_parse_inline_component_triggers(data))
    out.extend(_parse_light_effects(data))
    return out


# ---------------------------------------------------------------------------
# Per-shape parsers
# ---------------------------------------------------------------------------


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
            conditions=[],
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
            conditions=[],
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
                conditions=[],
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


# ---------------------------------------------------------------------------
# Tree decomposition
# ---------------------------------------------------------------------------


def _decompose_trigger_body(body: Any, *, trigger_id: str) -> AutomationTree:
    """Build an :class:`AutomationTree` from a trigger handler's body.

    Three YAML shortcut forms collapse into one tree:

    1. ``on_press: - light.turn_on: id`` (bare action list)
    2. ``on_press: light.turn_on: id`` (single bare action)
    3. ``on_press: then: - light.turn_on: id`` (explicit then)

    The trigger's own parameters (``on_click.min_length`` etc.) and
    its optional ``condition:`` gate live on the resulting
    :class:`AutomationTree` regardless of shortcut form.
    """
    trigger_params: dict[str, Any] = {}
    conditions: list[ConditionNode] = []
    actions: list[ActionNode] = []

    if body is None:
        return AutomationTree(
            trigger_id=trigger_id,
            trigger_params={},
            conditions=[],
            actions=[],
        )

    if isinstance(body, list):
        # Bare action list shortcut.
        actions = _decompose_action_list(body)
    elif isinstance(body, dict):
        # Explicit form with ``then:`` and/or trigger params.
        trigger_params = _collect_block_params(body, action_list_keys={"then"})
        cond_value = body.get("condition")
        if cond_value is not None:
            conditions = _decompose_condition_list(cond_value)
        if "then" in body:
            actions = _decompose_action_list(body["then"])
        else:
            # Bare single-action shortcut — every key that doesn't
            # match a known trigger param is an action.
            action_keys = [k for k in body if k != "condition" and k not in trigger_params]
            if action_keys:
                actions = _decompose_action_list([body])
                trigger_params = {}

    return AutomationTree(
        trigger_id=trigger_id,
        trigger_params=trigger_params,
        conditions=conditions,
        actions=actions,
    )


def _decompose_action_list(body: Any) -> list[ActionNode]:
    """Recursively turn a YAML action-list body into a list of nodes.

    Accepts a list of action dicts, a single action dict, or
    ``None``. Each list item is a single-entry mapping
    ``{<action_id>: <params>}`` per ESPHome's registry shape.
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
        # Templated / shortcut form — e.g. ``light.turn_on: id``
        # where the value is a bare id literal. Surface as a single
        # ``id`` parameter so the round-trip writer reconstructs the
        # short form when no other params are set.
        params = {"id": _render_value(raw_params)}

    return ActionNode(
        action_id=action_id,
        params=params,
        children=children,
        conditions=conditions,
    )


def _decompose_condition_list(body: Any) -> list[ConditionNode]:
    """Turn a condition / and / or / not body into a list of nodes."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_block_params(
    block: dict,
    *,
    action_list_keys: set[str],
) -> dict[str, Any]:
    """Collect non-then / non-action keys as plain ``params`` values."""
    out: dict[str, Any] = {}
    for key, value in block.items():
        if key in action_list_keys or key == "condition":
            continue
        out[key] = _render_value(value)
    return out


def _render_value(value: Any) -> Any:
    """
    Convert a ruamel-parsed value into the JSON-wire shape.

    Handles three special cases:

    - ``LiteralScalarString`` / ``FoldedScalarString`` (the
      ``!lambda |- ...`` / ``|`` blocks) → ``{"_lambda": "<source>"}``
      sentinel so the frontend can distinguish a lambda from a
      literal string.
    - ruamel ordered mappings → plain dicts (mashumaro doesn't speak
      ``CommentedMap`` directly).
    - ruamel lists → plain lists, recursively.
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
    """Cheap title-case of an ``on_x_y`` key for display labels."""
    return key.replace("_", " ").title()


def _key_range(mapping: Any, key: str) -> tuple[int, int]:
    """Return 1-indexed line range covering ``mapping[key]``.

    ruamel attaches ``lc`` (LineCol) metadata to every block-style
    container; ``.data[key]`` carries the ``(key_line, key_col,
    value_line, value_col)`` tuple of one entry. The end line falls
    back to the start when ruamel can't compute one (single-line
    flow-style entries).
    """
    lc = getattr(mapping, "lc", None)
    if lc is None or not getattr(lc, "data", None) or key not in lc.data:
        return 1, 1
    key_line, _key_col, _val_line, _val_col = lc.data[key]
    start = key_line + 1
    end = _estimate_end_line(mapping[key], start)
    return start, end


def _item_range(seq: Any, idx: int) -> tuple[int, int]:
    """Return 1-indexed line range for the *idx*'th list item.

    ``seq.lc.data[idx]`` carries the dash-line / dash-col / value-
    line / value-col tuple; we use the dash line so blank or
    comment lines preceding the item don't shift the range.
    """
    lc = getattr(seq, "lc", None)
    if lc is None or not getattr(lc, "data", None) or idx not in lc.data:
        return 1, 1
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
                    # entries are (key_line, key_col, val_line, val_col)
                    if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                        max_line = max(max_line, entry[2] + 1)
        elif isinstance(node, list):
            stack.extend(node)
            data = getattr(lc, "data", None) if lc else None
            if data:
                for entry in data.values():
                    if isinstance(entry, (list, tuple)) and len(entry) >= 3:
                        max_line = max(max_line, entry[2] + 1)
    return max_line


def _dump_slice(value: Any) -> str:
    """Serialise *value* through the round-trip emitter as a YAML string."""
    yaml = make_yaml()
    buf = StringIO()
    yaml.dump(value, buf)
    return buf.getvalue()
