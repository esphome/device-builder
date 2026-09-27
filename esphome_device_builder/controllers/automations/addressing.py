"""Which names more than one item of a config carries, and the writes refused for it."""

from __future__ import annotations

from collections import Counter
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


class Names:
    """How many items of one loaded config carry each name an automation is addressed by."""

    def __init__(self, root: Any) -> None:
        scripts = _scripts(root)
        self.scripts = Counter(script_id for script_id, _item in scripts)
        self.unnamed_scripts = [script_id for script_id, item in scripts if not declares_id(item)]
        self.api_actions = Counter(_api_action_names(root))
        instances = [(domain, comp_id) for domain, _, comp_id, _ in iter_instance_targets(root)]
        self.instances = Counter(comp_id for _domain, comp_id in instances)
        self.lights = Counter(comp_id for domain, comp_id in instances if domain == "light")

    @classmethod
    def of(cls, yaml_text: str) -> Names:
        """Count the names of *yaml_text*; none when it does not load."""
        try:
            root = make_yaml().load(yaml_text)
        except Exception:  # noqa: BLE001 — the writer reports a config that does not load
            root = None
        return cls(root)

    def named(self, location: AutomationLocation) -> tuple[str, str, int] | None:
        """Return what *location* names an item by, the name and how many carry it."""
        if isinstance(location, ScriptLocation):
            return "id", location.id, self.scripts[location.id]
        if isinstance(location, ApiActionLocation):
            return "action name", location.action_name, self.api_actions[location.action_name]
        if isinstance(location, LightEffectLocation):
            return "id", location.component_id, self.lights[location.component_id]
        if isinstance(location, (ComponentOnLocation, ComponentActionFieldLocation)):
            return "id", location.component_id, self.instances[location.component_id]
        return None

    def shares(self, location: AutomationLocation) -> bool:
        """Report whether more than one item carries the name of *location*."""
        named = self.named(location)
        return named is not None and named[2] > 1


def require_writable(
    yaml_text: str, location: AutomationLocation, *, declared_only: bool = False
) -> None:
    """Raise ``PRECONDITION_FAILED`` unless *location* names one item, declared if so asked."""
    names = Names.of(yaml_text)
    named = names.named(location)
    if named is None:
        return
    what, name, count = named
    if count > 1:
        msg = (
            f"more than one item in this config is named '{name}'; give each its own {what} "
            "in the YAML, then try again. Nothing was written."
        )
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
    if declared_only and isinstance(location, ScriptLocation) and name in names.unnamed_scripts:
        msg = (
            f"'{name}' is the id a script without an id is listed under; pick another id, or "
            "give that script an id in the YAML. Nothing was written."
        )
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)


def _scripts(root: Any) -> list[tuple[str, dict]]:
    """Return the ``script:`` items of *root*, each with the id it is listed under."""
    scripts = root.get("script") if isinstance(root, dict) else None
    if not isinstance(scripts, list):
        return []
    return [
        (instance_id("script", item, idx, is_list=True), item)
        for idx, item in enumerate(scripts)
        if is_mapping_entry(item)
    ]


def _api_action_names(root: Any) -> list[str]:
    """Return the name of every ``api:`` action of *root*."""
    api = root.get("api") if isinstance(root, dict) else None
    if not isinstance(api, dict):
        return []
    actions = api.get("actions")
    if not isinstance(actions, list):
        actions = api.get("services")
    if not isinstance(actions, list):
        return []
    return [
        str(item.get("action") or item.get("service") or "")
        for item in actions
        if is_mapping_entry(item)
    ]
