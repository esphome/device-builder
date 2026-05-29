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

import asyncio
import logging
from collections.abc import Callable
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Any, TypedDict

from mashumaro.mixins.orjson import DataClassORJSONMixin

from ...helpers.json import loads as json_loads
from ...helpers.lazy_catalog import LazyBodyStore, is_unsafe_catalog_id
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


_LOGGER = logging.getLogger(__name__)

_DEFINITIONS_PACKAGE = "esphome_device_builder.definitions"
_INDEX_FILE = "automations.index.json"
_BODIES_PACKAGE = "esphome_device_builder.definitions.automations"

# Bounded LRU per type — sized to comfortably hold a typical
# automation editor session (one form open at a time touches ~10
# bodies including referenced triggers / actions). 128 matches
# the components catalog default.
_BODY_CACHE_MAXSIZE = 128


def _load_body_from_disk[BodyT: DataClassORJSONMixin](
    type_key: str, body_cls: type[BodyT]
) -> Callable[[str], BodyT | None]:
    """Return a ``load_one(id) -> BodyT | None`` reader for one sub-catalog."""

    def _load(catalog_id: str) -> BodyT | None:
        if is_unsafe_catalog_id(catalog_id):
            _LOGGER.warning("Refusing %s body for traversal-shaped id: %r", type_key, catalog_id)
            return None
        try:
            raw = (
                resources.files(_BODIES_PACKAGE)
                .joinpath(type_key)
                .joinpath(f"{catalog_id}.json")
                .read_bytes()
            )
        except (FileNotFoundError, ModuleNotFoundError):
            return None
        return body_cls.from_dict(json_loads(raw))

    return _load


@cache
def _load_index() -> dict[str, Any]:
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
    parsed: dict[str, Any] = json_loads(raw_bytes)
    return parsed


def _build_slim[SlimT: DataClassORJSONMixin](type_key: str, slim_cls: type[SlimT]) -> list[SlimT]:
    return [slim_cls.from_dict(e) for e in _load_index().get(type_key, [])]


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


# Frozen id sets feed each store's ``is_known`` gate. Bound as
# ``frozenset.__contains__`` so the hot path is one C-level
# dispatch rather than a lambda + cached function call.
_TRIGGER_IDS: frozenset[str] = frozenset(t.id for t in _slim_triggers())
_ACTION_IDS: frozenset[str] = frozenset(a.id for a in _slim_actions())
_CONDITION_IDS: frozenset[str] = frozenset(c.id for c in _slim_conditions())
_LIGHT_EFFECT_IDS: frozenset[str] = frozenset(e.id for e in _slim_light_effects())
_FILTER_IDS: frozenset[str] = frozenset(f.id for f in _slim_filters())

# Per-type lazy body stores. Each store reads its bodies from
# ``definitions/automations/<type>/<id>.json`` through the
# corresponding ``_load_body_from_disk`` reader. ``is_known``
# short-circuits unknown ids before touching the wheel resources.
_TRIGGER_STORE: LazyBodyStore[AutomationTrigger] = LazyBodyStore(
    load_one=_load_body_from_disk("triggers", AutomationTrigger),
    cache_maxsize=_BODY_CACHE_MAXSIZE,
    is_known=_TRIGGER_IDS.__contains__,
)
_ACTION_STORE: LazyBodyStore[AutomationAction] = LazyBodyStore(
    load_one=_load_body_from_disk("actions", AutomationAction),
    cache_maxsize=_BODY_CACHE_MAXSIZE,
    is_known=_ACTION_IDS.__contains__,
)
_CONDITION_STORE: LazyBodyStore[AutomationCondition] = LazyBodyStore(
    load_one=_load_body_from_disk("conditions", AutomationCondition),
    cache_maxsize=_BODY_CACHE_MAXSIZE,
    is_known=_CONDITION_IDS.__contains__,
)
_LIGHT_EFFECT_STORE: LazyBodyStore[LightEffect] = LazyBodyStore(
    load_one=_load_body_from_disk("light_effects", LightEffect),
    cache_maxsize=_BODY_CACHE_MAXSIZE,
    is_known=_LIGHT_EFFECT_IDS.__contains__,
)
_FILTER_STORE: LazyBodyStore[Filter] = LazyBodyStore(
    load_one=_load_body_from_disk("filters", Filter),
    cache_maxsize=_BODY_CACHE_MAXSIZE,
    is_known=_FILTER_IDS.__contains__,
)

# Wire ``type`` field on ``automations/get_bodies`` refs -> store.
# ``LazyBodyStore[Any]`` widens past the invariant per-type parameter;
# the bulk fetch only needs ``to_dict()`` which every concrete body
# type implements via ``DataClassORJSONMixin``.
_STORES_BY_TYPE: dict[str, LazyBodyStore[Any]] = {
    "triggers": _TRIGGER_STORE,
    "actions": _ACTION_STORE,
    "conditions": _CONDITION_STORE,
    "light_effects": _LIGHT_EFFECT_STORE,
    "filters": _FILTER_STORE,
}


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
# Async batched body fetch — single executor hop across all types.
# ---------------------------------------------------------------------------


class AutomationBodyRef(TypedDict):
    """Wire-shape entry in the ``automations/get_bodies`` ``refs`` list."""

    type: str
    id: str


async def get_bodies(refs: list[AutomationBodyRef]) -> dict[str, dict]:
    """Resolve a batch of ``{type, id}`` refs to the wire response.

    Single executor hop covers every cross-type miss. Cache hits
    return without IO. Unknown types, unknown ids, and missing-on-
    disk bodies are absent from the response. Duplicate
    ``(type, id)`` pairs collapse to one entry. Response is keyed
    by ``"<type>/<id>"``.

    Trades :class:`LazyBodyStore`'s same-id ``asyncio.Lock``
    coalescing for the single-hop cross-store batch — two
    concurrent calls with overlapping refs each pay their own
    disk read. Acceptable because reads are idempotent and the
    cache writes are GIL-atomic; do not re-introduce the lock
    here without restoring per-store ``get_many`` (which
    re-introduces the per-type executor hops).
    """
    result: dict[str, dict] = {}
    misses: list[tuple[str, str, LazyBodyStore[Any]]] = []
    seen: set[tuple[str, str]] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        type_key = ref.get("type", "")
        cid = ref.get("id", "")
        if not type_key or not cid or (type_key, cid) in seen:
            continue
        seen.add((type_key, cid))
        store = _STORES_BY_TYPE.get(type_key)
        if store is None or not store.is_known(cid):
            continue
        cached = store.try_get_cached(cid)
        if cached is not None:
            result[f"{type_key}/{cid}"] = cached.to_dict()
            continue
        misses.append((type_key, cid, store))

    if not misses:
        return result

    def _load_all() -> list[tuple[str, str, Any]]:
        return [(t, cid, store.load_one_sync(cid)) for t, cid, store in misses]

    loaded = await asyncio.to_thread(_load_all)
    for type_key, cid, body in loaded:
        if body is None:
            continue
        _STORES_BY_TYPE[type_key].cache_put(cid, body)
        result[f"{type_key}/{cid}"] = body.to_dict()
    return result


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
