"""
Tree → ruamel data structures.

Turns :class:`AutomationTree` / :class:`ActionNode` /
:class:`ConditionNode` into ruamel ``CommentedMap`` / ``CommentedSeq``
shapes. The ``render_*`` entry points return a YAML *string* so the
writer only indents and splices strings — it never sees ruamel
internals.

Two ergonomic shortcuts the emitter applies on fresh writes (the
parser accepts both shapes, so the choice is purely cosmetic):

- An action with one param ``id`` and no children / conditions
  renders as ``- <action_id>: <id>`` (registry shortcut) instead
  of the explicit ``{id: <id>}`` mapping.
- A condition list of length one collapses to the single condition
  mapping.
"""

from __future__ import annotations

from io import StringIO
from typing import Any

from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString

from ...models.automations import (
    ActionNode,
    AutomationAction,
    AutomationCondition,
    AutomationTree,
    ConditionNode,
    LightEffect,
)
from . import catalog
from .parsing import DEFAULT_SHORTHAND_KEY, make_yaml


def render_script_item(tree: AutomationTree, script_id: str) -> str:
    """Render a single ``- id: ...`` script list item."""
    item = CommentedMap()
    item["id"] = script_id
    for key, value in tree.trigger_params.items():
        if key == "id":
            continue
        item[key] = encode_value(value)
    item["then"] = emit_action_seq(tree.actions)
    return dump([item])


def render_interval_item(tree: AutomationTree) -> str:
    """Render a single ``- interval: ...`` interval list item."""
    return dump([emit_trigger_list_item(tree)])


def render_api_action_item(tree: AutomationTree, action_name: str) -> str:
    """Render a single ``- action: <name>`` api-actions list item."""
    item = CommentedMap()
    item["action"] = action_name
    for key, value in tree.trigger_params.items():
        if key in ("action", "service"):
            continue
        item[key] = encode_value(value)
    item["then"] = emit_action_seq(tree.actions)
    return dump([item])


def render_trigger_handler(tree: AutomationTree, *, key: str) -> str:
    """
    Render a ``<trigger_key>:`` mapping with then + trigger params.

    Canonicalises to the explicit ``then:`` form on every write so
    round-trips stay deterministic (the parser accepts both
    shortcut shapes too).
    """
    wrapper = CommentedMap()
    wrapper[key] = emit_trigger_list_item(tree)
    return dump(wrapper)


def render_action_field(tree: AutomationTree, *, key: str) -> str:
    """
    Render ``<field>:`` as a bare action list (no ``then:`` wrapper).

    Component action-list config fields (cover ``open_action`` …) take
    a plain action list, unlike trigger handlers which canonicalise to
    the ``then:`` form. Trigger-less, so no params are emitted — only
    the action sequence (reusing :func:`emit_action_seq`).
    """
    wrapper = CommentedMap()
    wrapper[key] = emit_action_seq(tree.actions)
    return dump(wrapper)


def emit_trigger_list_item(tree: AutomationTree) -> CommentedMap:
    """Build one entry mapping (trigger params plus ``then:``) for a list-shaped trigger."""
    item = CommentedMap()
    for key, value in tree.trigger_params.items():
        item[key] = encode_value(value)
    item["then"] = emit_action_seq(tree.actions)
    return item


def emit_action_seq(actions: list[ActionNode]) -> CommentedSeq:
    """Build a ruamel sequence of single-key action mappings."""
    seq = CommentedSeq()
    for node in actions:
        seq.append(emit_action_node(node))
    return seq


def _shorthand_key(entry: AutomationAction | AutomationCondition | None) -> str:
    """Return the single-param key that collapses to a bare-scalar form."""
    if entry is not None and entry.scalar_shorthand_key:
        return entry.scalar_shorthand_key
    return DEFAULT_SHORTHAND_KEY


def emit_action_node(node: ActionNode) -> CommentedMap:
    """Build one ``{<action_id>: <body>}`` mapping for an action node."""
    body = CommentedMap()
    for key, value in node.params.items():
        body[key] = encode_value(value)
    for child_key in sorted(node.children.keys(), key=lambda k: (k != "then", k)):
        body[child_key] = emit_action_seq(node.children[child_key])
    if node.conditions:
        body["condition"] = emit_condition_seq(node.conditions)
    out = CommentedMap()
    shorthand = _shorthand_key(catalog.action_by_id(node.action_id))
    if (
        not node.children
        and not node.conditions
        and len(node.params) == 1
        and shorthand in node.params
    ):
        out[node.action_id] = encode_value(node.params[shorthand])
        return out
    if not body:
        out[node.action_id] = None
        return out
    out[node.action_id] = body
    return out


def emit_condition_seq(conditions: list[ConditionNode]) -> Any:
    """Build a ruamel sequence (or single mapping) of condition entries."""
    rendered = [emit_condition_node(c) for c in conditions]
    if len(rendered) == 1:
        return rendered[0]
    seq = CommentedSeq()
    for item in rendered:
        seq.append(item)
    return seq


def emit_condition_node(node: ConditionNode) -> CommentedMap:
    """Build one ``{<condition_id>: <body>}`` mapping for a condition node."""
    out = CommentedMap()
    if node.children:
        out[node.condition_id] = emit_condition_seq(node.children)
        return out
    if not node.params:
        out[node.condition_id] = None
        return out
    shorthand = _shorthand_key(catalog.condition_by_id(node.condition_id))
    if len(node.params) == 1 and shorthand in node.params:
        out[node.condition_id] = encode_value(node.params[shorthand])
        return out
    body = CommentedMap()
    for key, value in node.params.items():
        body[key] = encode_value(value)
    out[node.condition_id] = body
    return out


def emit_effect_item(effect: LightEffect | None, effect_id: str, params: dict) -> CommentedMap:
    """Build one ``{<effect_id>: <params>}`` mapping for an effects list."""
    del effect  # currently unused — kept for future schema-driven defaults
    body = CommentedMap()
    if isinstance(params, dict):
        for key, value in params.items():
            body[key] = encode_value(value)
    out = CommentedMap()
    out[effect_id] = body or None
    return out


def encode_value(value: Any) -> Any:
    """
    Encode a JSON-wire value back into a ruamel-native scalar.

    The lambda sentinel (``{"_lambda": "..."}``) becomes a
    :class:`LiteralScalarString` (``|`` block scalar); nested
    dicts and lists recurse.
    """
    # ESPHome treats unquoted ``|`` block scalars on templatable
    # fields as lambdas without needing an explicit ``!lambda``
    # tag — matching that shape on fresh writes so a freshly-emitted
    # YAML doesn't gain a tag the user wouldn't have typed. The
    # parser still detects both bare-block and ``!lambda``-tagged
    # forms, so a hand-tagged input round-trips intact.
    if (
        isinstance(value, dict)
        and set(value.keys()) == {"_lambda"}
        and isinstance(value["_lambda"], str)
    ):
        body = value["_lambda"]
        if not body.endswith("\n"):
            body += "\n"
        return LiteralScalarString(body)
    if isinstance(value, dict):
        out = CommentedMap()
        for k, v in value.items():
            out[k] = encode_value(v)
        return out
    if isinstance(value, list):
        seq = CommentedSeq()
        for v in value:
            seq.append(encode_value(v))
        return seq
    return value


def dump(value: Any) -> str:
    """Serialise *value* through the round-trip emitter."""
    yaml = make_yaml()
    buf = StringIO()
    yaml.dump(value, buf)
    return buf.getvalue()
