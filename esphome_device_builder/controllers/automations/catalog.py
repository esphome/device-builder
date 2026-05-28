"""Automation catalog loader.

Eagerly loads the slim ``definitions/automations.index.json`` at
module import; full bodies hydrate lazily on first access through
per-type :class:`LazyBodyStore` caches. The previous monolithic
``automations.json`` (~15.9 MB) is no longer read by the runtime —
the slim index is ~336 KB and bodies pay only the memory of what
parsing / writing actually touch.

The module-level functions stay for back-compat with parsing and
writing's existing sync call sites; ``all_*`` returns slim entries
(used by the WS list endpoints) and ``*_by_id`` returns full
bodies (used by parsing / writing to access ``config_entries``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING

from ...helpers.lazy_catalog import LazyBodyStore
from ...models.automations import (
    AutomationAction,
    AutomationActionIndex,
    AutomationCondition,
    AutomationConditionIndex,
    AutomationTrigger,
    AutomationTriggerIndex,
    Filter,
    FilterIndex,
    LightEffect,
    LightEffectIndex,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


_DEFINITIONS_PACKAGE = "esphome_device_builder.definitions"
_INDEX_FILE = "automations.index.json"
_BODIES_PACKAGE = "esphome_device_builder.definitions.automations"

# Bounded LRU per type — sized to comfortably hold a typical
# automation editor session (one form open at a time touches ~10
# bodies including referenced triggers / actions). 128 matches
# the components catalog default.
_BODY_CACHE_MAXSIZE = 128


def _load_body_from_disk[BodyT](
    type_key: str, body_cls: type[BodyT]
) -> Callable[[str], BodyT | None]:
    """Return a ``load_one(id) -> BodyT | None`` reader for one sub-catalog."""

    def _load(catalog_id: str) -> BodyT | None:
        try:
            raw = (
                resources.files(_BODIES_PACKAGE)
                .joinpath(type_key)
                .joinpath(f"{catalog_id}.json")
                .read_bytes()
            )
        except (FileNotFoundError, ModuleNotFoundError):
            return None
        return body_cls.from_dict(json.loads(raw))  # type: ignore[attr-defined]

    return _load


@cache
def _load_index() -> dict:
    """Read the slim ``automations.index.json`` once at first access."""
    try:
        raw_bytes = resources.files(_DEFINITIONS_PACKAGE).joinpath(_INDEX_FILE).read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return {
            "triggers": [],
            "actions": [],
            "conditions": [],
            "light_effects": [],
            "filters": [],
        }
    return json.loads(raw_bytes)


def _build_slim[SlimT](type_key: str, slim_cls: type[SlimT]) -> list[SlimT]:
    return [slim_cls.from_dict(e) for e in _load_index().get(type_key, [])]  # type: ignore[attr-defined]


# Slim in-memory state (matches the wire shape the WS list
# endpoints ship). Built lazily on first access; ``@cache``-d so
# the rebuild cost is paid once per process.
@cache
def _slim_triggers() -> list[AutomationTriggerIndex]:
    return _build_slim("triggers", AutomationTriggerIndex)


@cache
def _slim_actions() -> list[AutomationActionIndex]:
    return _build_slim("actions", AutomationActionIndex)


@cache
def _slim_conditions() -> list[AutomationConditionIndex]:
    return _build_slim("conditions", AutomationConditionIndex)


@cache
def _slim_light_effects() -> list[LightEffectIndex]:
    return _build_slim("light_effects", LightEffectIndex)


@cache
def _slim_filters() -> list[FilterIndex]:
    return _build_slim("filters", FilterIndex)


@cache
def _slim_index_ids(type_key: str) -> frozenset[str]:
    """Frozen set of known ids by type, the LazyBodyStore ``is_known`` gate."""
    return frozenset(e["id"] for e in _load_index().get(type_key, []))


# Per-type lazy body stores. Each store reads its bodies from
# ``definitions/automations/<type>/<id>.json`` through the
# corresponding ``_load_body_from_disk`` reader. ``is_known``
# short-circuits unknown ids before touching the wheel resources.
_TRIGGER_STORE: LazyBodyStore[AutomationTrigger] = LazyBodyStore(
    load_one=_load_body_from_disk("triggers", AutomationTrigger),
    cache_maxsize=_BODY_CACHE_MAXSIZE,
    is_known=lambda cid: cid in _slim_index_ids("triggers"),
)
_ACTION_STORE: LazyBodyStore[AutomationAction] = LazyBodyStore(
    load_one=_load_body_from_disk("actions", AutomationAction),
    cache_maxsize=_BODY_CACHE_MAXSIZE,
    is_known=lambda cid: cid in _slim_index_ids("actions"),
)
_CONDITION_STORE: LazyBodyStore[AutomationCondition] = LazyBodyStore(
    load_one=_load_body_from_disk("conditions", AutomationCondition),
    cache_maxsize=_BODY_CACHE_MAXSIZE,
    is_known=lambda cid: cid in _slim_index_ids("conditions"),
)
_LIGHT_EFFECT_STORE: LazyBodyStore[LightEffect] = LazyBodyStore(
    load_one=_load_body_from_disk("light_effects", LightEffect),
    cache_maxsize=_BODY_CACHE_MAXSIZE,
    is_known=lambda cid: cid in _slim_index_ids("light_effects"),
)
_FILTER_STORE: LazyBodyStore[Filter] = LazyBodyStore(
    load_one=_load_body_from_disk("filters", Filter),
    cache_maxsize=_BODY_CACHE_MAXSIZE,
    is_known=lambda cid: cid in _slim_index_ids("filters"),
)


# ---------------------------------------------------------------------------
# Slim list accessors — picker fields only, wire shape for the WS list endpoints.
# ---------------------------------------------------------------------------


def all_triggers() -> list[AutomationTriggerIndex]:
    """Return the slim trigger catalog (picker fields, no config_entries)."""
    return list(_slim_triggers())


def all_actions() -> list[AutomationActionIndex]:
    """Return the slim action catalog (picker fields, no config_entries)."""
    return list(_slim_actions())


def all_conditions() -> list[AutomationConditionIndex]:
    """Return the slim condition catalog."""
    return list(_slim_conditions())


def all_light_effects() -> list[LightEffectIndex]:
    """Return the slim light-effects catalog."""
    return list(_slim_light_effects())


def all_filters() -> list[FilterIndex]:
    """Return the slim filter catalog."""
    return list(_slim_filters())


# ---------------------------------------------------------------------------
# Full-body accessors — sync, lazy-loaded with LRU caching. Used by
# parsing / writing on a worker thread to access ``config_entries``.
# ---------------------------------------------------------------------------


def trigger_by_id(trigger_id: str) -> AutomationTrigger | None:
    """Look up one trigger's full body by qualified id (e.g. ``binary_sensor.on_press``)."""
    return _TRIGGER_STORE.get_sync(trigger_id)


def action_by_id(action_id: str) -> AutomationAction | None:
    """Look up one action's full body by qualified id (e.g. ``light.turn_on``)."""
    return _ACTION_STORE.get_sync(action_id)


def condition_by_id(condition_id: str) -> AutomationCondition | None:
    """Look up one condition's full body by qualified id."""
    return _CONDITION_STORE.get_sync(condition_id)


def light_effect_by_id(effect_id: str) -> LightEffect | None:
    """Look up one light effect's full body by bare id."""
    return _LIGHT_EFFECT_STORE.get_sync(effect_id)


# ---------------------------------------------------------------------------
# Async full-body accessors — for WS handlers serving detail-view requests.
# ---------------------------------------------------------------------------


async def get_trigger_body(trigger_id: str) -> AutomationTrigger | None:
    """Async-load one trigger's full body via the body store."""
    return await _TRIGGER_STORE.get(trigger_id)


async def get_action_body(action_id: str) -> AutomationAction | None:
    """Async-load one action's full body via the body store."""
    return await _ACTION_STORE.get(action_id)


async def get_condition_body(condition_id: str) -> AutomationCondition | None:
    """Async-load one condition's full body via the body store."""
    return await _CONDITION_STORE.get(condition_id)


async def get_light_effect_body(effect_id: str) -> LightEffect | None:
    """Async-load one light-effect's full body via the body store."""
    return await _LIGHT_EFFECT_STORE.get(effect_id)


async def get_filter_body(filter_id: str) -> Filter | None:
    """Async-load one filter's full body via the body store."""
    return await _FILTER_STORE.get(filter_id)


# ---------------------------------------------------------------------------
# Domain-scoped slim filters — used by the WS picker endpoints.
# ---------------------------------------------------------------------------


def triggers_for_domains(domains: Iterable[str]) -> list[AutomationTriggerIndex]:
    """Device-level triggers + every trigger applying to *domains*."""
    domain_set = set(domains)
    device_level: list[AutomationTriggerIndex] = []
    component: list[AutomationTriggerIndex] = []
    for trigger in _slim_triggers():
        if trigger.is_device_level:
            device_level.append(trigger)
            continue
        if any(d in domain_set for d in trigger.applies_to):
            component.append(trigger)
    return device_level + component


def actions_for_domains(domains: Iterable[str]) -> list[AutomationActionIndex]:
    """``core`` actions + every action whose ``domain`` is in *domains*."""
    return _filter_by_domain_slim(_slim_actions(), set(domains))


def conditions_for_domains(domains: Iterable[str]) -> list[AutomationConditionIndex]:
    """``core`` conditions + every condition whose ``domain`` is in *domains*."""
    return _filter_by_domain_slim(_slim_conditions(), set(domains))


def _filter_by_domain_slim[T: (AutomationActionIndex, AutomationConditionIndex)](
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


# Pre-warm the slim index at module-import time so the first
# request never trips blockbuster on the disk read.
_load_index()
_slim_triggers()
_slim_actions()
_slim_conditions()
_slim_light_effects()
_slim_filters()


# Back-compat: the legacy ``load_catalog()`` shape the old controller
# imported. The runtime no longer uses it but tests and external
# callers might; returns the slim shapes (the old call site was
# only used for ``all_triggers()`` etc. which themselves now return
# slim).
def load_catalog() -> dict[str, list]:
    """Return the five slim catalog lists keyed by section."""
    return {
        "triggers": list(_slim_triggers()),
        "actions": list(_slim_actions()),
        "conditions": list(_slim_conditions()),
        "light_effects": list(_slim_light_effects()),
        "filters": list(_slim_filters()),
    }
