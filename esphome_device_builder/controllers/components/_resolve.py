"""Featured-registry records, entry materialisation, and body loaders."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ...helpers.json import loads
from ...helpers.lazy_catalog import is_unsafe_catalog_id
from ...models import (
    ComponentCatalogEntry,
    ComponentCatalogIndexEntry,
    ComponentCategory,
    ConfigEntry,
    FeaturedComponent,
    FieldPreset,
)

# Prefix used to route featured-component IDs to the featured registry.
# Format: ``featured.<board_id>.<local_id>`` (e.g. ``featured.sonoff-basic.relay``).
_FEATURED_PREFIX = "featured."

_LOGGER = logging.getLogger(__name__)

_DEFINITIONS_DIR = Path(__file__).resolve().parent.parent.parent / "definitions"
_COMPONENTS_INDEX_JSON = _DEFINITIONS_DIR / "components.index.json"
_COMPONENT_BODIES_DIR = _DEFINITIONS_DIR / "components"

# Bounded LRU for hydrated component bodies. The catalog ships ~900
# bodies totalling ~22MB on disk; pinning every loaded body would
# bring back the eager-load memory cost the split was meant to drop.
# Sized to absorb a navigator's full-device batch (typical
# ~30-50 components) plus headroom for featured-card warmups and
# yaml-completion lookups without thrashing.
_BODY_CACHE_MAXSIZE = 128

# Catalog ids for components that ESPHome auto-loads as transport /
# helper modules but that the dashboard's Add Configuration picker
# should not surface as user-facing choices. ESPHome pulls these in
# automatically when the user adds the public-facing component (e.g.
# adding ``web_server:`` causes ESPHome to also load ``web_server_idf``
# / ``web_server_base`` based on the framework). Listing them here is
# harmless if a user does add one explicitly — ESPHome's own validator
# accepts the form — but they're confusing noise in the picker.
#
# Tradeoff: hand-curated rather than derived from each component's
# ``auto_load`` chain. Deriving would auto-track new internals as
# ESPHome adds them, but every legitimate user-facing component that
# *also* appears in some other component's auto_load list (network,
# wifi via captive_portal, etc.) would need an opt-out exception —
# and missing one of those filters out a real choice. Hand-curated
# fails closed: missing an internal here just leaves a confusing-but-
# harmless extra option, which the user explicitly preferred ("better
# to manually exclude than miss one — these are rare edge cases",
# issue #325). Extend by adding to the set; a JSON regen via
# ``script/sync_components.py`` is not required for this filter to
# take effect.
#
# Public (non-underscore) name because ``script/sync_components.py``
# imports this constant so the generator and the runtime loader
# share one source of truth — extending the denylist edits one set,
# not two.
INTERNAL_COMPONENT_IDS: frozenset[str] = frozenset(
    {
        "web_server_base",
        "web_server_idf",
    }
)


# ---------------------------------------------------------------------------
# Featured registry
# ---------------------------------------------------------------------------


@dataclass
class _FeaturedRecord:
    """
    A featured-component manifest entry resolved against the catalog index.

    ``underlying_id`` is the regular catalog id the user is actually
    adding (``switch.gpio``, ...); ``featured`` carries the manifest's
    name/description overrides and per-field presets to layer on top.
    The body (config_entries tree) is fetched on demand via
    :meth:`ComponentCatalog.get_body`.
    """

    full_id: str
    board_id: str
    featured: FeaturedComponent
    underlying_id: str


def _materialise_featured_index(
    record: _FeaturedRecord,
    underlying: ComponentCatalogIndexEntry,
) -> ComponentCatalogIndexEntry:
    """
    Return the slim card-view representation of *record*.

    Builds a :class:`ComponentCatalogIndexEntry` with the synthetic
    ``featured.<board>.<local>`` id and category ``featured``,
    overlaying the manifest's name/description (and keeping the
    underlying component's image / dependencies / supported_platforms).
    """
    fc = record.featured
    return ComponentCatalogIndexEntry(
        id=record.full_id,
        name=fc.name or underlying.name,
        description=fc.description if fc.description is not None else underlying.description,
        category=ComponentCategory.FEATURED,
        docs_url=underlying.docs_url,
        image_url=underlying.image_url,
        dependencies=list(underlying.dependencies),
        multi_conf=underlying.multi_conf,
        supported_platforms=list(underlying.supported_platforms),
    )


def _materialise_featured(
    record: _FeaturedRecord,
    underlying: ComponentCatalogEntry,
    target_platform: str | None,
) -> ComponentCatalogEntry:
    """
    Return *record* as a full ``ComponentCatalogEntry`` ready for the detail API.

    The result carries the synthetic ``featured.<board>.<local>`` id and
    category ``featured``, the manifest's name/description overrides, and
    each ``FieldPreset`` baked into the corresponding ``ConfigEntry`` as
    ``default_value`` / ``locked`` / ``suggestions``.
    """
    fc = record.featured
    presets = fc.fields
    return ComponentCatalogEntry(
        id=record.full_id,
        name=fc.name or underlying.name,
        description=fc.description if fc.description is not None else underlying.description,
        category=ComponentCategory.FEATURED,
        docs_url=underlying.docs_url,
        image_url=underlying.image_url,
        dependencies=list(underlying.dependencies),
        multi_conf=underlying.multi_conf,
        is_list=underlying.is_list,
        supported_platforms=list(underlying.supported_platforms),
        config_entries=[
            _materialise_entry_with_preset(entry, target_platform, presets.get(entry.key))
            for entry in underlying.config_entries
        ],
    )


def _materialise_entry_with_preset(
    entry: ConfigEntry,
    target_platform: str | None,
    preset: FieldPreset | None,
) -> ConfigEntry:
    """
    Return *entry* materialised for *target_platform* with *preset* applied.

    ``preset.value`` overrides ``default_value``, ``preset.locked`` and
    ``preset.suggestions`` ride through to the returned entry. Without a
    preset this is equivalent to :func:`_materialise_entry`.
    """
    base = _materialise_entry(entry, target_platform)
    if preset is None:
        return base
    if preset.value is not None:
        base.default_value = preset.value  # type: ignore[assignment]
    base.locked = preset.locked
    if preset.suggestions is not None:
        base.suggestions = list(preset.suggestions)
    return base


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _as_category_set(value: ComponentCategory | str | list[str]) -> set[str]:
    """Normalise a category filter into a set of plain strings.

    Accepts a single ``ComponentCategory`` / string or a list of
    strings — returns the set of raw category names used by
    ``ComponentCatalogEntry.category`` for membership tests.
    """
    if isinstance(value, list):
        return {str(v) for v in value}
    return {str(value)}


def _materialise(
    component: ComponentCatalogEntry,
    target_platform: str | None,
) -> ComponentCatalogEntry:
    """
    Return a copy of *component* with platform_defaults resolved.

    When *target_platform* is given, every config entry's
    ``platform_defaults`` map is consulted: if the platform is listed,
    that value replaces ``default_value``. The ``platform_defaults``
    field itself is always cleared in the returned copy so the API
    surface stays simple — the frontend just reads ``default_value``.
    """
    return ComponentCatalogEntry(
        id=component.id,
        name=component.name,
        description=component.description,
        category=component.category,
        docs_url=component.docs_url,
        image_url=component.image_url,
        dependencies=component.dependencies,
        multi_conf=component.multi_conf,
        is_list=component.is_list,
        supported_platforms=component.supported_platforms,
        config_entries=[_materialise_entry(e, target_platform) for e in component.config_entries],
    )


def _materialise_entry(entry: ConfigEntry, target_platform: str | None) -> ConfigEntry:
    """
    Resolve platform_defaults into default_value for *target_platform*.

    The returned entry never carries platform_defaults — that field is
    a sync-time implementation detail the frontend doesn't need to
    know about. Recurses into ``config_entries`` for nested entries
    so the resolution applies at every depth.
    """
    default = entry.default_value
    if target_platform and entry.platform_defaults and target_platform in entry.platform_defaults:
        default = entry.platform_defaults[target_platform]
    nested = (
        [_materialise_entry(e, target_platform) for e in entry.config_entries]
        if entry.config_entries
        else None
    )
    return ConfigEntry(
        key=entry.key,
        type=entry.type,
        label=entry.label,
        description=entry.description,
        required=entry.required,
        default_value=default,
        platform_defaults=None,
        options=entry.options,
        allow_custom_value=entry.allow_custom_value,
        range=entry.range,
        display_format=entry.display_format,
        registry=entry.registry,
        unit_options=entry.unit_options,
        multi_value=entry.multi_value,
        templatable=entry.templatable,
        depends_on=entry.depends_on,
        depends_on_value=entry.depends_on_value,
        depends_on_value_not=entry.depends_on_value_not,
        depends_on_component=entry.depends_on_component,
        references_component=entry.references_component,
        pin_features=entry.pin_features,
        pin_mode=entry.pin_mode,
        advanced=entry.advanced,
        hidden=entry.hidden,
        help_link=entry.help_link,
        translation_key=entry.translation_key,
        translation_params=entry.translation_params,
        config_entries=nested,
        platform_type=entry.platform_type,
        supported_platforms=list(entry.supported_platforms),
        group=entry.group,
        required_groups=list(entry.required_groups),
    )


# ---------------------------------------------------------------------------
# JSON → model loaders
# ---------------------------------------------------------------------------


def _load_body_from_disk(component_id: str) -> ComponentCatalogEntry | None:
    """Read ``components/<component_id>.json`` and hydrate into a ComponentCatalogEntry.

    Defense-in-depth traversal guard via
    :func:`is_unsafe_catalog_id` — kept as a string check rather
    than ``Path.resolve`` so the hot path stays out of the kernel
    ``lstat`` walk that resolve does on every hydrate.
    """
    if is_unsafe_catalog_id(component_id):
        _LOGGER.warning("Refusing component body for traversal-shaped id: %r", component_id)
        return None
    body_path = _COMPONENT_BODIES_DIR / f"{component_id}.json"
    if not body_path.is_file():
        _LOGGER.warning("Component body missing on disk: %s", body_path)
        return None
    return ComponentCatalogEntry.from_dict(loads(body_path.read_bytes()))
