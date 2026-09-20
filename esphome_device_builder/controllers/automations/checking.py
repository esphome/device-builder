"""Check an automation tree against the catalog before the emitter renders it."""

from __future__ import annotations

from typing import Any

from ...helpers.api import CommandError
from ...models import ErrorCode
from ...models.automations import (
    ActionNode,
    AutomationAction,
    AutomationCondition,
    AutomationTree,
    ConditionNode,
)
from . import catalog
from ._decompose import accepted_param_keys
from .catalog import AutomationBodyRef


async def check_tree(tree: AutomationTree) -> None:
    """Raise ``INVALID_ARGS`` for an unknown action or condition, or a field it cannot take."""
    refs: list[AutomationBodyRef] = []
    _collect_action_refs(tree.actions, refs)
    if refs:
        await catalog.get_bodies(refs)  # warms the per-id caches the sync lookups read
    _check_actions(tree.actions)


def _collect_action_refs(nodes: list[ActionNode], refs: list[AutomationBodyRef]) -> None:
    for node in nodes:
        if node.unknown:
            continue
        refs.append({"type": "actions", "id": node.action_id})
        for branch in node.children.values():
            _collect_action_refs(branch, refs)
        _collect_condition_refs(node.conditions, refs)


def _collect_condition_refs(nodes: list[ConditionNode], refs: list[AutomationBodyRef]) -> None:
    for node in nodes:
        refs.append({"type": "conditions", "id": node.condition_id})
        _collect_condition_refs(node.children, refs)


def _check_actions(nodes: list[ActionNode]) -> None:
    for node in nodes:
        if node.unknown:
            continue
        if not catalog.is_known_action(node.action_id):
            raise CommandError(ErrorCode.INVALID_ARGS, f"Unknown action id {node.action_id!r}")
        # A known but not form-editable action has no body to check fields against.
        if (entry := catalog.action_by_id(node.action_id)) is not None:
            _check_fields(node.action_id, node.params, entry)
            if stray := sorted(set(node.children) - set(entry.accepts_action_list)):
                msg = (
                    f"Action {node.action_id!r} has no {stray} branch; "
                    f"it takes {entry.accepts_action_list}"
                )
                raise CommandError(ErrorCode.INVALID_ARGS, msg)
        for branch in node.children.values():
            _check_actions(branch)
        _check_conditions(node.conditions)


def _check_conditions(nodes: list[ConditionNode]) -> None:
    for node in nodes:
        entry = catalog.condition_by_id(node.condition_id)
        if entry is None:
            msg = f"Unknown condition id {node.condition_id!r}"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        _check_fields(node.condition_id, node.params, entry)
        if node.children and not entry.accepts_condition_list:
            msg = f"Condition {node.condition_id!r} takes no nested conditions"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)
        _check_conditions(node.children)


def _check_fields(
    node_id: str, params: dict[str, Any], entry: AutomationAction | AutomationCondition
) -> None:
    if (allowed := accepted_param_keys(entry)) is None:
        return
    if unknown := sorted(set(params) - allowed):
        msg = f"{node_id!r} has no field {unknown}; its fields are {sorted(allowed)}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
