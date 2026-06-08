"""
Trigger-handler body → :class:`AutomationTree` decomposition.

Turns a ruamel-parsed handler body (bare action list, single bare
action, or explicit ``then:``) into the typed tree the frontend edits,
normalising every per-automation fault (unknown action / condition id)
to :class:`CommandError` so the collector can contain it to one entry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ruamel.yaml.comments import TaggedScalar
from ruamel.yaml.scalarfloat import ScalarFloat
from ruamel.yaml.scalarstring import LiteralScalarString

from ...helpers.api import CommandError
from ...models.api import ErrorCode
from ...models.automations import ActionNode, AutomationTree, ConditionNode
from . import catalog

# Action-body keys that introduce a condition gate rather than plain params.
_CONDITION_GATE_KEYS: frozenset[str] = frozenset({"condition", "all", "any"})

# Fallback shorthand key when a catalog entry has no ``scalar_shorthand_key``
# (id-reference actions / conditions). Shared with the emitter's collapse check.
DEFAULT_SHORTHAND_KEY = "id"


def _safe_tree(
    build: Callable[[], AutomationTree], *, trigger_id: str | None
) -> tuple[AutomationTree, str | None]:
    """
    Run *build*, isolating a per-automation decompose failure.

    The decompose helpers normalise every per-automation fault to
    ``CommandError`` (unknown action / condition id), so catching it
    here contains the fault to this one entry — it comes back with an
    empty tree plus the message while its siblings parse. A document
    that won't load at all is the separate whole-file failure raised by
    :func:`parse_device_yaml` upstream.
    """
    try:
        return build(), None
    except CommandError as err:
        return AutomationTree(trigger_id=trigger_id, trigger_params={}, actions=[]), err.message


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
        # Single-action shortcut: the body's keys are a mix of
        # trigger params and known catalog action ids.
        # ``_collect_block_params`` naively absorbed both; pull
        # the action keys back out by catalog lookup and rebuild
        # ``trigger_params`` without them.
        action_body = {k: v for k, v in body.items() if catalog.action_by_id(k) is not None}
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
            if key in _CONDITION_GATE_KEYS:
                conditions = _decompose_condition_list(value)
                continue
            params[key] = _render_value(value)
    else:
        # Bare-scalar shorthand (``logger.log: "hi"`` / ``light.turn_on: id``):
        # surface the scalar under the action's own ``maybe_simple_value`` key
        # so the writer reconstructs the short form on round-trip.
        key = action.scalar_shorthand_key or DEFAULT_SHORTHAND_KEY
        # ``core.wait_until`` has ``maybe == "condition"``; a shorthand that
        # names a gate / sub-list key must never land in ``params`` — fall
        # back to ``id`` so it round-trips harmlessly.
        if key in _CONDITION_GATE_KEYS or key in action.accepts_action_list:
            key = DEFAULT_SHORTHAND_KEY
        params = {key: _render_value(raw_params)}

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
        key = catalog_entry.scalar_shorthand_key or DEFAULT_SHORTHAND_KEY
        params = {key: _render_value(value)}
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

    Lambda block scalars become the ``{"_lambda": "<source>"}``
    sentinel; an ``!lambda``-tagged value additionally carries
    ``"_tag": "!lambda"`` so the emitter re-emits the tag (dropping it
    turns the C++ lambda into a plain string literal). ruamel maps and
    lists become plain dicts/lists, recursively. Tagged scalars from
    ruamel are not JSON-serialisable on their own, so any unrecognised
    tag falls back to its plain string value.
    """
    if isinstance(value, LiteralScalarString):
        return {"_lambda": str(value)}
    if isinstance(value, TaggedScalar):
        tag = getattr(value.tag, "value", "") if value.tag is not None else ""
        if tag == "!lambda":
            return {"_lambda": str(value), "_tag": "!lambda"}
        return str(value)
    if isinstance(value, dict):
        return {k: _render_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_value(v) for v in value]
    # ruamel round-trip mode wraps floats in ScalarFloat (a float subclass);
    # orjson serialises int/bool subclasses but refuses float subclasses, so
    # coerce to a plain float for the wire.
    return float(value) if isinstance(value, ScalarFloat) else value


def _render_params(value: Any) -> Any:
    """Wrap an arbitrary ruamel value as a plain dict for ``params``."""
    rendered = _render_value(value)
    if isinstance(rendered, dict):
        return rendered
    return {"_value": rendered}
