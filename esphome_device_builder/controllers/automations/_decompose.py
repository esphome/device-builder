"""
Trigger-handler body → :class:`AutomationTree` decomposition.

Turns a ruamel-parsed handler body (bare action list, single bare
action, or explicit ``then:``) into the typed tree the frontend edits.
An uncatalogued action becomes an opaque passthrough node (its
siblings stay editable); a still-fatal per-automation fault (unknown
condition id, malformed entry) normalises to :class:`CommandError` so
the collector can contain it to one entry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar
from ruamel.yaml.scalarfloat import ScalarFloat
from ruamel.yaml.scalarstring import LiteralScalarString

from ...helpers.api import CommandError
from ...helpers.automation_keys import (
    BRANCH_ACTIONS_KEY,
    BRANCH_CONDITION_KEY,
    BRANCH_ELSE_KEY,
    COMBINATOR_GATE_KEYS,
    CONDITION_GATE_KEYS,
    is_scalar_bodied,
    scalar_param_key,
)
from ...helpers.yaml.scalar import is_custom_yaml_tag
from ...models.api import ErrorCode
from ...models.automations import (
    ActionNode,
    AutomationAction,
    AutomationCondition,
    AutomationTree,
    ChooseBranch,
    ConditionNode,
)
from . import catalog


class UnsupportedActionError(CommandError):
    """A body the tree cannot represent (formless LVGL ``*.update``, a tagged condition gate)."""


def _safe_tree(
    build: Callable[[], AutomationTree], *, trigger_id: str | None
) -> tuple[AutomationTree, str | None, bool]:
    """
    Run *build*, isolating a per-automation decompose failure.

    The decompose helpers normalise a still-fatal per-automation fault
    to ``CommandError`` (unknown condition id, malformed entry), so
    catching it here contains the fault to this one entry — it comes
    back with an empty tree plus the message while its siblings parse.
    (An uncatalogued action no longer faults — it decomposes to an
    opaque passthrough node.) A document that won't load at all is the
    separate whole-file failure raised by :func:`parse_device_yaml`
    upstream. The third tuple field flags an
    :class:`UnsupportedActionError` — a body the tree cannot represent.
    """
    try:
        return build(), None, False
    except UnsupportedActionError as err:
        empty = AutomationTree(trigger_id=trigger_id, trigger_params={}, actions=[])
        return empty, err.message, True
    except CommandError as err:
        empty = AutomationTree(trigger_id=trigger_id, trigger_params={}, actions=[])
        return empty, err.message, False


def _block_tree(trigger_params: dict[str, Any], then_body: Any) -> AutomationTree:
    """Build the trigger-less tree for a ``script:`` / ``interval:`` / ``api.actions:`` item."""
    return AutomationTree(
        trigger_id=None,
        trigger_params=trigger_params,
        actions=_decompose_action_list(then_body),
    )


def _decompose_trigger_body(body: Any, *, trigger_id: str | None) -> AutomationTree:
    """
    Build an :class:`AutomationTree` from a trigger handler's body.

    Accepts three YAML shortcut forms that all collapse to the same
    tree: bare action list (``on_press: - action: ...``), single
    bare action (``on_press: action: ...``), explicit ``then:``.

    ``trigger_id`` is ``None`` for trigger-less action-list config
    fields (cover ``open_action`` …); the tree then carries no trigger.
    """
    if isinstance(body, dict):
        return _decompose_trigger_mapping(body, trigger_id=trigger_id)
    actions = _decompose_action_list(body) if isinstance(body, list) else []
    return AutomationTree(trigger_id=trigger_id, trigger_params={}, actions=actions)


def _decompose_trigger_mapping(body: dict[str, Any], *, trigger_id: str | None) -> AutomationTree:
    """
    Decompose one mapping-form trigger handler (params + ``then:``).

    Splits the mapping into trigger params and its action list,
    accepting the explicit ``then:`` form and the single-action
    shortcut. Reused per list entry for list-shaped triggers.
    """
    trigger_params = _collect_block_params(body, action_list_keys={"then"})
    if "then" in body:
        actions = _decompose_action_list(body["then"])
    else:
        # Single-action shortcut: the body mixes trigger params and
        # action ids. Pull the action keys back out and rebuild
        # ``trigger_params`` without them. Match on ``is_known_action``,
        # not ``action_by_id``, so a known-but-non-editable LVGL
        # ``*.update`` reaches the UnsupportedActionError fallback
        # instead of being mislabelled a trigger param.
        action_body = {k: v for k, v in body.items() if catalog.is_known_action(k)}
        actions = []
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
    for raw_item in items:
        # ``- script.stop``: a bare string is that action with an empty body.
        item = {raw_item: None} if isinstance(raw_item, str) else raw_item
        if not isinstance(item, dict) or not item or isinstance(item, TaggedScalar):
            # Anything else (a tag, a number, an empty entry) would vanish on save.
            msg = f"Action list item the editor cannot represent: {_describe_item(raw_item)}"
            raise UnsupportedActionError(ErrorCode.INVALID_ARGS, msg)
        # A registry-shape action is a single-key ``{id: params}`` mapping. A
        # multi-key item is a misrouted / malformed body (a typo'd trigger
        # entry that fell through to the action-list path), so an uncatalogued
        # id there must fault rather than pass through — see _decompose_action.
        multi_key = len(item) > 1
        for action_id, params in item.items():
            out.append(_decompose_action(str(action_id), params, multi_key=multi_key))
    return out


def _describe_item(raw: Any) -> str:
    """Name an action-list item's shape for an error message."""
    if isinstance(raw, TaggedScalar):
        return f"{raw.tag.value} tagged value"
    if raw is None or raw == {}:
        return "empty entry"
    return type(raw).__name__


def _uncatalogued_action(action_id: str, raw_params: Any, *, multi_key: bool) -> ActionNode:
    """Handle an id with no catalog entry: passthrough node, or fault."""
    if catalog.is_known_action(action_id):
        msg = f"No structured editor for action {action_id!r}"
        raise UnsupportedActionError(ErrorCode.INVALID_ARGS, msg)
    if multi_key:
        # Uncatalogued id sharing a mapping with other keys: a real external
        # action is always a one-key entry, so this is a misrouted body.
        # Fault so a save can't silently restructure it.
        msg = f"Unknown action id: {action_id!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    # Uncatalogued single-key id (an external component's action, or a typo):
    # keep it as an opaque passthrough node so its siblings stay editable
    # rather than collapsing the whole automation to read-only. ``preserve_tags``
    # keeps ``!secret`` / ``!include`` etc. (scalar or collection) through the
    # emitter (comments and exact formatting are still lost); the frontend
    # shows it read-only.
    return ActionNode(
        action_id=action_id,
        unknown=True,
        raw_body=_render_value(raw_params, preserve_tags=True),
    )


def _decompose_action(action_id: str, raw_params: Any, *, multi_key: bool = False) -> ActionNode:
    """Build one :class:`ActionNode` from a registry-shaped mapping entry."""
    action = catalog.action_by_id(action_id)
    if action is None:
        return _uncatalogued_action(action_id, raw_params, multi_key=multi_key)
    children: dict[str, list[ActionNode]] = {}
    conditions: list[ConditionNode] = []
    branches: list[ChooseBranch] = []

    if raw_params is None:
        params: dict[str, Any] = {}
    elif action.has_branches:
        params = {}
        branches, else_actions = _decompose_branches(action_id, raw_params)
        if else_actions is not None:
            children[BRANCH_ELSE_KEY] = else_actions
    elif isinstance(raw_params, dict) and is_scalar_bodied(action):
        # ``delay: {seconds: 2}``: the mapping is the value's dict form, not fields.
        params = {scalar_param_key(action): _render_value(raw_params)}
    elif isinstance(raw_params, dict):
        params, children, conditions = _decompose_mapping_body(action, raw_params)
    elif (
        isinstance(raw_params, str)
        and action.scalar_shorthand_key in CONDITION_GATE_KEYS
        and catalog.is_known_condition(raw_params)
    ):
        # ``wait_until: api.connected``: esphome reads a string in a condition
        # position as that condition id with no config.
        conditions = _decompose_condition_list(raw_params)
        params = {}
    else:
        # Bare-scalar shorthand (``logger.log: "hi"`` / ``light.turn_on: id``):
        # store the scalar under the entry's collapse key so the writer
        # reconstructs the short form on round-trip.
        params = {scalar_param_key(action): _render_value(raw_params)}

    return ActionNode(
        action_id=action_id,
        params=params,
        children=children,
        conditions=conditions,
        branches=branches,
    )


def _decompose_mapping_body(
    action: AutomationAction, body: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, list[ActionNode]], list[ConditionNode]]:
    """Split a mapping body into ``(params, children, conditions)``."""
    if _is_dict_shorthand_condition(action, body):
        # ``wait_until: {api.connected:}`` — the ``condition:`` wrapper is
        # optional (ESPHome ``maybe_simple_value``); the whole mapping is
        # the condition. Without this the condition id lands in ``params``
        # and the gate renders empty.
        return {}, {}, _decompose_condition_list(body)
    params: dict[str, Any] = {}
    children: dict[str, list[ActionNode]] = {}
    conditions: list[ConditionNode] = []
    gate_key: str | None = None
    for key, value in body.items():
        if key in action.accepts_action_list:
            children[key] = _decompose_action_list(value)
            continue
        if key in CONDITION_GATE_KEYS:
            if gate_key is not None:
                msg = f"{action.id!r} carries both {gate_key!r} and {key!r}"
                raise CommandError(ErrorCode.INVALID_ARGS, msg)
            gate_key = key
            conditions = _decompose_gate(key, value)
            continue
        params[key] = _render_value(value)
    if gate_key is None and action.has_condition_gate:
        conditions = _pop_bare_condition(action, body, params)
    return params, children, conditions


def _decompose_gate(key: str, value: Any) -> list[ConditionNode]:
    """Turn a ``condition`` / ``all`` / ``any`` gate body into the node's conditions."""
    if key in COMBINATOR_GATE_KEYS:
        return [ConditionNode(condition_id=key, children=_decompose_condition_list(value))]
    return _decompose_condition_list(value)


def _pop_bare_condition(
    action: AutomationAction, body: dict[str, Any], params: dict[str, Any]
) -> list[ConditionNode]:
    """Lift a condition id written directly under a gated action (``if: {lambda: ...}``)."""
    known = {e.key for e in action.config_entries}
    bare = [k for k in params if k not in known and catalog.is_known_condition(str(k))]
    if not bare:
        return []
    if len(bare) > 1:
        msg = f"Only one condition may be given directly under {action.id!r}, found {bare}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    key = bare[0]
    del params[key]
    return _decompose_condition_list({key: body[key]})


def _decompose_branches(
    action_id: str, raw: Any
) -> tuple[list[ChooseBranch], list[ActionNode] | None]:
    """Turn a ``choose`` body into its ordered branches plus the trailing ``else`` list."""
    items = raw if isinstance(raw, list) else [raw]
    branches: list[ChooseBranch] = []
    else_actions: list[ActionNode] | None = None
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            msg = f"{action_id!r} entry {idx} must be a mapping"
            raise UnsupportedActionError(ErrorCode.INVALID_ARGS, msg)
        if BRANCH_ELSE_KEY in item:
            if idx != len(items) - 1 or len(item) != 1:
                msg = f"{action_id!r} takes {BRANCH_ELSE_KEY!r} only as its last entry, alone"
                raise UnsupportedActionError(ErrorCode.INVALID_ARGS, msg)
            else_actions = _decompose_action_list(item[BRANCH_ELSE_KEY])
            continue
        if set(item) != {BRANCH_CONDITION_KEY, BRANCH_ACTIONS_KEY}:
            needed = f"{BRANCH_CONDITION_KEY!r} and {BRANCH_ACTIONS_KEY!r}"
            msg = f"{action_id!r} entry {idx} takes exactly {needed}"
            raise UnsupportedActionError(ErrorCode.INVALID_ARGS, msg)
        branches.append(
            ChooseBranch(
                conditions=_decompose_condition_list(item[BRANCH_CONDITION_KEY]),
                actions=_decompose_action_list(item[BRANCH_ACTIONS_KEY]),
            )
        )
    return branches, else_actions


def _is_dict_shorthand_condition(action: AutomationAction, body: dict[str, Any]) -> bool:
    """Whether *body* is a ``wait_until``-style condition with the gate key omitted."""
    if not body or action.scalar_shorthand_key not in CONDITION_GATE_KEYS:
        return False
    keys = body.keys()
    if keys & CONDITION_GATE_KEYS:
        return False
    known = {e.key for e in action.config_entries} | set(action.accepts_action_list)
    return not (keys & known)


def _decompose_condition_list(body: Any) -> list[ConditionNode]:
    """Turn a ``condition`` / ``and`` / ``or`` / ``not`` body into nodes."""
    if body is None:
        return []
    if isinstance(body, list):
        return [_decompose_condition(item) for item in body]
    return [_decompose_condition(body)]


def _condition_params(entry: AutomationCondition, value: Any) -> dict[str, Any]:
    """Return the params a leaf condition's *value* carries."""
    if value is None:
        return {}
    if isinstance(value, dict) and not is_scalar_bodied(entry):
        return {k: _render_value(v) for k, v in value.items()}
    return {scalar_param_key(entry): _render_value(value)}


def _decompose_condition(raw: Any) -> ConditionNode:
    """Build one :class:`ConditionNode` from a registry-shaped entry or a bare condition id."""
    if isinstance(raw, str):
        raw = {raw: None}
    if isinstance(raw, TaggedScalar):
        msg = "Condition uses a YAML tag the editor cannot represent"
        raise UnsupportedActionError(ErrorCode.INVALID_ARGS, msg)
    if not isinstance(raw, dict):
        msg = "Condition must be a mapping or a condition id"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    if not raw:
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
    else:
        params = _condition_params(catalog_entry, value)
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


def _render_value(value: Any, *, preserve_tags: bool = False) -> Any:
    """
    Convert a ruamel-parsed value to its JSON-wire shape.

    Lambda block scalars become the ``{"_lambda": "<source>"}``
    sentinel; an ``!lambda``-tagged value additionally carries
    ``"_tag": "!lambda"`` so the emitter re-emits the tag (dropping it
    turns the C++ lambda into a plain string literal). ruamel maps and
    lists become plain dicts/lists, recursively. Any other tagged value
    (scalar or collection) falls back untagged — *unless* ``preserve_tags``
    (an opaque passthrough body), where it becomes a ``{"_tagged", "_tag"}``
    sentinel the emitter re-tags, so ``!secret`` / ``!include`` inside an
    uncatalogued action survive a re-emit. (Comments and exact formatting
    are still lost — ruamel rebuilds the document.)
    """
    if isinstance(value, LiteralScalarString):
        return {"_lambda": str(value)}
    if isinstance(value, TaggedScalar):
        return _render_tagged_scalar(value, preserve_tags=preserve_tags)
    if isinstance(value, dict):
        rendered = {k: _render_value(v, preserve_tags=preserve_tags) for k, v in value.items()}
        return _maybe_tagged(value, rendered, preserve_tags=preserve_tags)
    if isinstance(value, list):
        items = [_render_value(v, preserve_tags=preserve_tags) for v in value]
        return _maybe_tagged(value, items, preserve_tags=preserve_tags)
    return _render_scalar_leaf(value, preserve_tags=preserve_tags)


def _render_scalar_leaf(value: Any, *, preserve_tags: bool) -> Any:
    """Coerce a scalar leaf to a JSON-wire value (see :func:`_render_value`)."""
    # ruamel round-trip mode wraps floats in ScalarFloat (a float subclass);
    # orjson serialises int/bool subclasses but refuses float subclasses, so
    # coerce to a plain float for the wire.
    if isinstance(value, ScalarFloat):
        return float(value)
    if preserve_tags and not isinstance(value, (str, int, float, type(None))):
        # A standard-tag scalar (``!!binary`` → bytes, ``!!set`` → a set) would
        # otherwise ride into raw_body and make orjson refuse the whole parse
        # response. Keep it JSON-safe. (bool is an int subclass, so it stays.)
        return str(value)
    return value


def _render_tagged_scalar(value: TaggedScalar, *, preserve_tags: bool) -> Any:
    """Render a ruamel ``TaggedScalar`` to its wire shape (see :func:`_render_value`)."""
    tag = value.tag.value
    if tag == "!lambda":
        return {"_lambda": str(value), "_tag": "!lambda"}
    if preserve_tags and is_custom_yaml_tag(tag):
        return {"_tagged": str(value), "_tag": tag}
    return str(value)


def _maybe_tagged(node: Any, rendered: Any, *, preserve_tags: bool) -> Any:
    """Wrap *rendered* in a ``{_tagged, _tag}`` sentinel iff *node* is a tagged ruamel map/seq."""
    if preserve_tags and isinstance(node, (CommentedMap, CommentedSeq)):
        tag = node.tag.value
        if is_custom_yaml_tag(tag):
            return {"_tagged": rendered, "_tag": tag}
    return rendered


def _render_params(value: Any) -> Any:
    """Wrap an arbitrary ruamel value as a plain dict for ``params``."""
    rendered = _render_value(value)
    if isinstance(rendered, dict):
        return rendered
    return {"_value": rendered}
