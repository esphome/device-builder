"""
Automation catalog loader.

Reads ``definitions/automations.json`` once at module-import time
and caches the four parsed lists. The catalog ships frozen inside
the wheel — no runtime regeneration, no live ``esphome`` import on
the request path.
"""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING

from ...models.automations import (
    AutomationAction,
    AutomationCondition,
    AutomationTrigger,
    Filter,
    LightEffect,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


_DEFINITIONS_PACKAGE = "esphome_device_builder.definitions"
_CATALOG_FILE = "automations.json"


@cache
def load_catalog() -> dict[str, list]:
    """
    Return the five catalog lists keyed by section.

    Keys: ``triggers`` / ``actions`` / ``conditions`` /
    ``light_effects`` / ``filters``. Empty lists when
    ``automations.json`` is missing — a fresh checkout that hasn't
    run ``script/sync_components.py`` yet boots cleanly with an
    empty catalog instead of crashing.
    """
    try:
        raw_bytes = resources.files(_DEFINITIONS_PACKAGE).joinpath(_CATALOG_FILE).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return {
            "triggers": [],
            "actions": [],
            "conditions": [],
            "light_effects": [],
            "filters": [],
        }
    raw = json.loads(raw_bytes)
    return {
        "triggers": [AutomationTrigger.from_dict(t) for t in raw.get("triggers", [])],
        "actions": [AutomationAction.from_dict(a) for a in raw.get("actions", [])],
        "conditions": [AutomationCondition.from_dict(c) for c in raw.get("conditions", [])],
        "light_effects": [LightEffect.from_dict(e) for e in raw.get("light_effects", [])],
        "filters": [Filter.from_dict(f) for f in raw.get("filters", [])],
    }


def all_triggers() -> list[AutomationTrigger]:
    """Return the full trigger catalogue."""
    return list(load_catalog()["triggers"])


def all_actions() -> list[AutomationAction]:
    """Return the full action catalogue."""
    return list(load_catalog()["actions"])


def all_conditions() -> list[AutomationCondition]:
    """Return the full condition catalogue."""
    return list(load_catalog()["conditions"])


def all_light_effects() -> list[LightEffect]:
    """Return the full light-effects catalogue."""
    return list(load_catalog()["light_effects"])


def all_filters() -> list[Filter]:
    """Return the full filter catalogue (sensor / binary_sensor / text_sensor)."""
    return list(load_catalog()["filters"])


def action_by_id(action_id: str) -> AutomationAction | None:
    """Look up one action by its qualified id (e.g. ``light.turn_on``)."""
    for action in all_actions():
        if action.id == action_id:
            return action
    return None


def condition_by_id(condition_id: str) -> AutomationCondition | None:
    """Look up one condition by its qualified id."""
    for condition in all_conditions():
        if condition.id == condition_id:
            return condition
    return None


def trigger_by_id(trigger_id: str) -> AutomationTrigger | None:
    """Look up one trigger by its qualified id."""
    for trigger in all_triggers():
        if trigger.id == trigger_id:
            return trigger
    return None


def light_effect_by_id(effect_id: str) -> LightEffect | None:
    """Look up one light effect by its bare id."""
    for effect in all_light_effects():
        if effect.id == effect_id:
            return effect
    return None


def triggers_for_domains(domains: Iterable[str]) -> list[AutomationTrigger]:
    """
    Return device-level triggers + every trigger applying to *domains*.

    Device-level triggers always come first (in catalogue order),
    followed by component-level triggers whose ``applies_to`` includes
    a member of *domains*.
    """
    domain_set = set(domains)
    device_level: list[AutomationTrigger] = []
    component: list[AutomationTrigger] = []
    for trigger in all_triggers():
        if trigger.is_device_level:
            device_level.append(trigger)
            continue
        if any(d in domain_set for d in trigger.applies_to):
            component.append(trigger)
    return device_level + component


def actions_for_domains(domains: Iterable[str]) -> list[AutomationAction]:
    """
    Return ``core`` actions + every action whose ``domain`` is in *domains*.

    ``core`` actions (``delay`` / ``lambda`` / ``if`` / ``while`` /
    ``repeat`` / ``wait_until``) come first (in catalogue order),
    followed by component actions whose canonical
    ``<domain>.<platform>`` (or bare ``<domain>``) id matches a
    qualified key in *domains*. Mirrors :func:`triggers_for_domains`.
    """
    return _filter_by_domain(all_actions(), set(domains))


def conditions_for_domains(domains: Iterable[str]) -> list[AutomationCondition]:
    """
    Return ``core`` conditions + every condition whose ``domain`` is in *domains*.

    ``core`` conditions (``and`` / ``or`` / ``all`` / ``any`` / ``not`` /
    ``xor`` / ``lambda`` / ``for``) come first (in catalogue order),
    followed by component conditions whose canonical
    ``<domain>.<platform>`` (or bare ``<domain>``) id matches a
    qualified key in *domains*.
    """
    return _filter_by_domain(all_conditions(), set(domains))


def _filter_by_domain[T: (AutomationAction, AutomationCondition)](
    items: list[T],
    domain_set: set[str],
) -> list[T]:
    """Partition *items* into core-first then component, by ``.domain``."""
    core: list[T] = []
    component: list[T] = []
    for item in items:
        if item.domain == "core":
            core.append(item)
        elif item.domain in domain_set:
            component.append(item)
    return core + component


# Pre-warm the catalog at module-import time so the first request
# never trips blockbuster on the disk read — same pattern the
# components catalog uses (``ComponentCatalog.load`` runs at
# controller construction, not on a request).
load_catalog()
