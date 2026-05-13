"""Automation catalog loader.

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
    LightEffect,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


_DEFINITIONS_PACKAGE = "esphome_device_builder.definitions"
_CATALOG_FILE = "automations.json"


@cache
def load_catalog() -> dict[str, list]:
    """
    Load and parse ``definitions/automations.json`` once per process.

    Returns a dict with four keys (``triggers`` / ``actions`` /
    ``conditions`` / ``light_effects``) holding the typed
    dataclasses. Empty lists when the bundle is missing — the
    runtime still answers the get_* commands, just with nothing in
    them. The sync script's failure mode is "no automations.json" so
    a fresh checkout without ``sync_components.py`` having run yet
    doesn't crash the dashboard on startup.
    """
    try:
        raw_bytes = resources.files(_DEFINITIONS_PACKAGE).joinpath(_CATALOG_FILE).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return {
            "triggers": [],
            "actions": [],
            "conditions": [],
            "light_effects": [],
        }
    raw = json.loads(raw_bytes)
    return {
        "triggers": [AutomationTrigger.from_dict(t) for t in raw.get("triggers", [])],
        "actions": [AutomationAction.from_dict(a) for a in raw.get("actions", [])],
        "conditions": [AutomationCondition.from_dict(c) for c in raw.get("conditions", [])],
        "light_effects": [LightEffect.from_dict(e) for e in raw.get("light_effects", [])],
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


def _prewarm() -> None:
    """Force the catalog into memory at module-import time.

    ``load_catalog`` reads the bundled ``automations.json`` from
    disk on the first call. Triggering it eagerly here pulls the I/O
    forward to module-import time so the runtime never trips
    blockbuster on the async request path — same shape the
    components catalog uses (``ComponentCatalog.load`` runs at
    controller construction, not on a request).
    """
    load_catalog()


_prewarm()


def triggers_for_domains(domains: Iterable[str]) -> list[AutomationTrigger]:
    """Return device-level triggers + every trigger applying to *domains*.

    Used by :meth:`AutomationsController.get_available` to filter the
    trigger catalogue down to what the user can actually use on
    their device — device-level triggers always apply, component-
    level triggers only when the matching domain is configured. The
    list is stable-sorted: device-level first (in catalogue order),
    then component triggers grouped by their applying domain.
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
