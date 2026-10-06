"""Refuse an automation write whose location does not name exactly the item it is for."""

from __future__ import annotations

from typing import Any

from ...helpers.api import CommandError
from ...models.api import ErrorCode
from ...models.automations import (
    ApiActionLocation,
    AutomationLocation,
    ComponentActionFieldLocation,
    ComponentOnLocation,
    LightEffectLocation,
    ScriptLocation,
)
from ._yaml import make_yaml
from .parsing import declares_id, instance_id, is_mapping_entry, iter_instance_targets

type _Named = (
    ScriptLocation
    | ApiActionLocation
    | ComponentOnLocation
    | ComponentActionFieldLocation
    | LightEffectLocation
)
_NAMED = (
    ScriptLocation,
    ApiActionLocation,
    ComponentOnLocation,
    ComponentActionFieldLocation,
    LightEffectLocation,
)


def require_writable(
    yaml_text: str, location: AutomationLocation, *, declared_only: bool = False
) -> None:
    """Raise ``PRECONDITION_FAILED`` unless *location* names one item, declared if so asked."""
    if not isinstance(location, _NAMED):
        return
    what, name, items = _items_named(_load(yaml_text), location)
    if len(items) > 1:
        msg = (
            f"more than one item in this config is named '{name}'; give each its own {what} "
            "in the YAML, then try again. Nothing was written."
        )
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
    if declared_only and isinstance(location, ScriptLocation) and not all(map(declares_id, items)):
        msg = (
            f"'{name}' is the id a script without an id is listed under; pick another id, or "
            "give that script an id in the YAML. Nothing was written."
        )
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)


def _load(yaml_text: str) -> Any:
    """Return the loaded *yaml_text*, or ``None`` when it does not load."""
    try:
        return make_yaml().load(yaml_text)
    except Exception:  # noqa: BLE001 — the writer reports a config that does not load
        return None


def _items_named(root: Any, location: _Named) -> tuple[str, str, list[dict]]:
    """Return what *location* names an item by, the name and the items of *root* carrying it."""
    if isinstance(location, ScriptLocation):
        return "id", location.id, _scripts_named(root, location.id)
    if isinstance(location, ApiActionLocation):
        return "action name", location.action_name, _api_actions_named(root, location.action_name)
    domain = "light" if isinstance(location, LightEffectLocation) else None
    return "id", location.component_id, _instances_named(root, location.component_id, domain)


def _scripts_named(root: Any, script_id: str) -> list[dict]:
    """Return the ``script:`` items of *root* listed under *script_id*."""
    scripts = root.get("script") if isinstance(root, dict) else None
    if not isinstance(scripts, list):
        return []
    return [
        item
        for idx, item in enumerate(scripts)
        if is_mapping_entry(item) and instance_id("script", item, idx, is_list=True) == script_id
    ]


def _api_actions_named(root: Any, action_name: str) -> list[dict]:
    """Return the ``api:`` actions of *root* named *action_name*."""
    api = root.get("api") if isinstance(root, dict) else None
    if not isinstance(api, dict):
        return []
    actions = api.get("actions")
    if not isinstance(actions, list):
        actions = api.get("services")
    if not isinstance(actions, list):
        return []
    return [
        item
        for item in actions
        if is_mapping_entry(item)
        and str(item.get("action") or item.get("service") or "") == action_name
    ]


def _instances_named(root: Any, component_id: str, domain: str | None = None) -> list[dict]:
    """Return the instances and sub-entities of *root* listed under *component_id*."""
    return [
        instance
        for found, instance, comp_id, _target in iter_instance_targets(root)
        if comp_id == component_id and domain in (None, found)
    ]
