"""Refuse an automation write whose location names more than one item of the config."""

from __future__ import annotations

from ...helpers.api import CommandError
from ...models.api import ErrorCode
from ...models.automations import ApiActionLocation, AutomationLocation, ScriptLocation
from ._yaml import make_yaml
from .parsing import _iter_instance_targets, parse_device_yaml


def require_unambiguous(yaml_text: str, location: AutomationLocation) -> None:
    """Raise ``PRECONDITION_FAILED`` when *location* names more than one item of *yaml_text*."""
    named = _named(location)
    if named is None:
        return
    what, name = named
    if _rows_at(yaml_text, location) > 1 or _instances_named(yaml_text, location) > 1:
        msg = (
            f"the {what} '{name}' names more than one item in this config, so a write could "
            "land on the wrong one; give each an id of its own, then list again. "
            "Nothing was written."
        )
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)


def _named(location: AutomationLocation) -> tuple[str, str] | None:
    """Return what *location* addresses an item by and that name, or ``None`` for a position."""
    if isinstance(location, ScriptLocation):
        return "id", location.id
    if isinstance(location, ApiActionLocation):
        return "action name", location.action_name
    component_id = getattr(location, "component_id", None)
    return None if component_id is None else ("id", component_id)


def _rows_at(yaml_text: str, location: AutomationLocation) -> int:
    """Count the automations of *yaml_text* listed at *location*; none when it does not load."""
    try:
        rows = parse_device_yaml(yaml_text)
    except CommandError as err:
        if err.code is not ErrorCode.INVALID_ARGS:
            raise
        return 0
    return sum(1 for row in rows if row.location == location)


def _instances_named(yaml_text: str, location: AutomationLocation) -> int:
    """Count the instances and sub-entities of *yaml_text* listed under the location's id."""
    component_id = getattr(location, "component_id", None)
    if component_id is None:
        return 0
    try:
        root = make_yaml().load(yaml_text)
    except Exception:  # noqa: BLE001 — the writer reports a config that does not load
        return 0
    return sum(
        1 for *_, comp_id, _target in _iter_instance_targets(root) if comp_id == component_id
    )
