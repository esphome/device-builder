"""Component catalog controller."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..helpers.api import api_command
from ..helpers.json import loads
from ..models import (
    ComponentCatalogEntry,
    ComponentCategory,
    ConfigEntry,
    ConfigEntryType,
    ConfigValueOption,
    FeaturedComponent,
    FieldPreset,
    PagedComponentsResponse,
    PinFeature,
    PinMode,
)

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

# Prefix used to route featured-component IDs to the featured registry.
# Format: ``featured.<board_id>.<local_id>`` (e.g. ``featured.sonoff-basic.relay``).
_FEATURED_PREFIX = "featured."

_LOGGER = logging.getLogger(__name__)

_COMPONENTS_JSON = Path(__file__).resolve().parent.parent / "definitions" / "components.json"

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


class ComponentCatalog:
    """In-memory component catalog with search and pagination."""

    def __init__(self, device_builder: DeviceBuilder | None = None) -> None:
        self._db = device_builder
        self._components: list[ComponentCatalogEntry] = []
        self._by_id: dict[str, ComponentCatalogEntry] = {}
        # Featured-component lookups, populated by ``_build_featured_registry``
        # after both catalogs have loaded. The ``_by_board`` index is what
        # lets ``get_components`` scope a ``category=featured`` query to one
        # board's recommendations rather than the whole catalog.
        self._featured_by_id: dict[str, _FeaturedRecord] = {}
        self._featured_by_board: dict[str, list[str]] = {}

    def load(self) -> None:
        """
        Load components from the pre-generated JSON file.

        Logs a warning and leaves the catalog empty when the file is
        missing — run ``script/sync_components.py`` to (re)generate it.
        """
        if not _COMPONENTS_JSON.exists():
            _LOGGER.warning(
                "Component catalog not found at %s — run script/sync_components.py",
                _COMPONENTS_JSON,
            )
            return

        # ``loads`` (orjson) decodes UTF-8 bytes directly — faster than
        # stdlib json on the ~896-component catalog and dodges the
        # platform-locale-encoding trap that bit Windows on read_text
        # without an explicit encoding.
        data = loads(_COMPONENTS_JSON.read_bytes())
        # Drop ESPHome internal-helper / auto-load-target components
        # — see ``INTERNAL_COMPONENT_IDS`` for the why.
        self._components = [
            _load_component(c)
            for c in data.get("components", [])
            if c.get("id") not in INTERNAL_COMPONENT_IDS
        ]
        self._by_id = {c.id: c for c in self._components}
        self._build_featured_registry()
        _LOGGER.info(
            "Component catalog loaded: %d components, %d featured",
            len(self._components),
            len(self._featured_by_id),
        )

    @property
    def categories(self) -> list[dict[str, str | int]]:
        """
        Return all component categories sorted by count (highest first).

        Each entry is a ``{id, name, count}`` dict suitable for direct
        use in the catalog UI's filter list.
        """
        return self._categories_for_board(None)

    @api_command("components/get_categories")
    async def get_categories(
        self,
        *,
        board_id: str | None = None,
        **kwargs: Any,
    ) -> list[dict[str, str | int]]:
        """
        Get all component categories with counts.

        When ``board_id`` is supplied, the response includes a synthetic
        ``featured`` entry whose count reflects the recommended components
        for that board (omitted entirely when the board has none).
        """
        return self._categories_for_board(board_id)

    @api_command("components/get_integration_docs")
    async def get_integration_docs(self, **kwargs: Any) -> dict[str, str]:
        """Return ``{integration_name: docs_url}`` for resolvable integrations.

        Returns a map covering every loaded-integration identifier we can
        resolve to an esphome.io docs page.

        ``loaded_integrations`` on a Device is a flat list of bare names
        (``api``, ``ledc``, ``ltr390``, ``sensor``) — the storage_json
        captures whatever ESPHome registered, with no category prefix.
        The catalog's ids are ``<category>.<stem>`` for category-scoped
        components and bare names for top-level ones, so we resolve by:

        1. Exact id match (``api`` → catalog id ``api``).
        2. Stem match (``ltr390`` → catalog id ``sensor.ltr390``); first
           hit wins when multiple categories share a stem.
        3. Category match (``sensor`` → ``https://esphome.io/components/sensor``,
           the parent path of any ``sensor.*`` component's docs URL).
           Only fills a slot a top-level component hasn't already claimed.

        Names with no catalog hit are simply omitted — the frontend
        renders them as plain text. The catalog's ``docs_url`` is sourced
        from the live esphome.io docs index, so a present URL is also a
        guarantee that the page exists.
        """
        # Three sources, applied in priority order:
        #   1. Top-level component (id without ``.``) — wins outright.
        #   2. Category landing — synthesised from any ``<cat>.<stem>``
        #      docs URL's parent path. ``switch`` in loaded_integrations
        #      means the switch *platform*, not the ``binary_sensor.switch``
        #      driver, so the category landing must beat the stem.
        #   3. Stem alias — picks up specific drivers like ``ltr390``
        #      (catalog id ``sensor.ltr390``) that aren't named anywhere
        #      else. Only used when every category in which the stem
        #      appears agrees on the docs URL — otherwise we'd silently
        #      pick one arbitrary page out of several conflicting ones
        #      (e.g. ``binary_sensor.gpio`` vs ``switch.gpio``), so the
        #      stem is dropped and the frontend renders it as plain
        #      text. "If we have a docs page for it" demands one
        #      unambiguous answer, not the first one we happen to see.
        top_level: dict[str, str] = {}
        category_urls: dict[str, str] = {}
        stem_candidates: dict[str, set[str]] = {}
        for comp in self._components:
            comp_id = comp.id
            docs = comp.docs_url
            if not comp_id or not docs:
                continue
            if "." not in comp_id:
                top_level[comp_id] = docs
                continue
            category, stem = comp_id.split(".", 1)
            # ESPHome's docs site serves a real index page at
            # ``/components/<category>/`` for every category that has
            # subcomponents. Derive it from the docs URL only when the
            # URL is genuinely under that path — some multi-platform
            # components (``switch.at581x`` → ``/components/at581x``)
            # are catalogued under a category for filtering but
            # documented at a top-level URL outside any category.
            marker = f"/components/{category}/"
            idx = docs.find(marker)
            if idx != -1:
                category_urls.setdefault(category, docs[: idx + len(marker) - 1])
            stem_candidates.setdefault(stem, set()).add(docs)

        # Stems are unambiguous only when every category that owns the
        # stem agrees on the same docs URL. Multi-platform components
        # (``at581x``, ``rotary_encoder``) hit this path because they
        # share a single docs page across categories.
        stems: dict[str, str] = {
            stem: next(iter(urls)) for stem, urls in stem_candidates.items() if len(urls) == 1
        }

        # ``dict.update()`` overwrites existing keys, so later writes
        # win. Apply lowest priority first (stems), then category, then
        # top-level — that way a colliding key is overridden by the
        # more-specific page.
        result: dict[str, str] = {}
        result.update(stems)
        result.update(category_urls)
        result.update(top_level)
        return result

    @api_command("components/get_component")
    async def get_component(
        self,
        *,
        component_id: str,
        platform: str | None = None,
        board_id: str | None = None,
        **kwargs: Any,
    ) -> ComponentCatalogEntry | None:
        """
        Get a single component by ID.

        When ``platform`` (or ``board_id``, which we resolve to a
        platform) is provided, ``platform_defaults`` are resolved
        into ``default_value`` for that target platform — frontend
        gets the right default without having to know the
        cv.SplitDefault details.

        ``component_id`` may also be a featured-component id of the form
        ``featured.<board>.<local>`` — the response then carries the
        underlying component with the board's ``FieldPreset`` overrides
        baked into ``default_value`` / ``locked`` / ``suggestions``.
        """
        if component_id.startswith(_FEATURED_PREFIX):
            record = self._featured_by_id.get(component_id)
            if record is None:
                return None
            # The featured id already encodes the board, so we pin platform
            # resolution to ``record.board_id``. A caller-supplied ``board_id``
            # that disagrees is almost certainly a bug — log it but don't
            # honour it (it'd resolve platform_defaults from the wrong board).
            if board_id is not None and board_id != record.board_id:
                _LOGGER.warning(
                    "Featured component %s requested with mismatched board_id %s; "
                    "resolving platform from %s",
                    component_id,
                    board_id,
                    record.board_id,
                )
            target_platform = self._resolve_platform(platform, record.board_id)
            return _materialise_featured(record, target_platform)

        target_platform = self._resolve_platform(platform, board_id)
        component = self._by_id.get(component_id)
        if component is None:
            return None
        return _materialise(component, target_platform)

    @api_command("components/get_components")
    async def get_components(
        self,
        *,
        query: str | None = None,
        category: ComponentCategory | str | list[str] | None = None,
        exclude_category: ComponentCategory | str | list[str] | None = None,
        platform: str | None = None,
        board_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
        **kwargs: Any,
    ) -> PagedComponentsResponse:
        """
        Get components with optional filtering, search, and pagination.

        ``query`` matches against the component id, name, and description.
        ``platform`` filters to components compatible with the given
        target platform — components with an empty ``supported_platforms``
        list are considered platform-agnostic and always included. When
        ``platform`` is set, each entry's ``platform_defaults`` map is
        also resolved into its ``default_value`` for that platform.

        ``board_id`` is a convenience: the boards catalog is consulted
        to derive the matching platform, so the frontend can pass
        whichever it has handy. ``platform`` wins when both are set.

        ``category`` and ``exclude_category`` accept either a single
        category or a list. ``exclude_category`` is the inverse used by
        the regular component selector to hide entries belonging to
        the dedicated "Add core configuration" dialog (``core``,
        plus the platform-domain umbrellas ``ota`` / ``time`` /
        ``update``). Both filters can be combined though that's
        unusual.

        Featured components are surfaced **only** when ``category``
        explicitly includes ``featured`` and ``board_id`` is set — the
        regular catalog listing never returns them. Mixed queries
        (e.g. ``category=["featured", "sensor"]``) return featured
        entries first followed by the matching regular entries.
        """
        target_platform = self._resolve_platform(platform, board_id)
        include_set = _as_category_set(category) if category else None
        exclude_set = _as_category_set(exclude_category) if exclude_category else None

        include_featured = (
            include_set is not None
            and ComponentCategory.FEATURED.value in include_set
            and board_id is not None
        )
        featured_entries = (
            self._featured_components_for_board(board_id, target_platform, query)
            if include_featured and board_id is not None
            else []
        )

        # Featured entries live in their own registry, never in
        # ``self._components``; strip the synthetic category before applying
        # the include filter so it doesn't filter out every regular entry.
        regular_include = (
            include_set - {ComponentCategory.FEATURED.value} if include_set is not None else None
        )

        if include_set is not None and not regular_include:
            results: list[ComponentCatalogEntry] = []
        else:
            results = self._components
            if regular_include:
                results = [c for c in results if c.category in regular_include]
            if exclude_set is not None:
                results = [c for c in results if c.category not in exclude_set]
            if target_platform:
                results = [
                    c
                    for c in results
                    if not c.supported_platforms or target_platform in c.supported_platforms
                ]
            if query:
                query_lower = query.lower()
                results = [
                    c
                    for c in results
                    if query_lower in c.name.lower()
                    or query_lower in c.description.lower()
                    or query_lower in c.id.lower()
                ]

        # Compose the page across both lists. Featured entries (already
        # materialised) come first; the regular slice is materialised lazily
        # so a wide query doesn't pay for entries the caller never reads.
        total_featured = len(featured_entries)
        total = total_featured + len(results)
        end = offset + limit
        page: list[ComponentCatalogEntry] = []
        if offset < total_featured:
            page.extend(featured_entries[offset : min(end, total_featured)])
        regular_start = max(0, offset - total_featured)
        regular_end = max(0, end - total_featured)
        if regular_end > regular_start:
            page.extend(
                _materialise(c, target_platform) for c in results[regular_start:regular_end]
            )

        return PagedComponentsResponse(
            components=page,
            total=total,
            offset=offset,
            limit=limit,
            # Sidebar counts share the request's filters so they reflect
            # what's actually findable. ``category`` is intentionally
            # left out — the user needs to see the *other* categories
            # to navigate between them.
            categories=self._categories_for_board(
                board_id,
                query=query,
                exclude_set=exclude_set,
                target_platform=target_platform,
            ),
        )

    def get_featured_record(self, component_id: str) -> _FeaturedRecord | None:
        """Return the registry record for a ``featured.*`` id, or ``None``."""
        return self._featured_by_id.get(component_id)

    def _build_featured_registry(self) -> None:
        """Walk the board catalog and index every featured component."""
        self._featured_by_id = {}
        self._featured_by_board = {}
        if self._db is None or self._db.boards is None:
            return
        for board in self._db.boards.iter_boards():
            ids: list[str] = []
            for fc in board.featured_components:
                full_id = f"{_FEATURED_PREFIX}{board.id}.{fc.id}"
                underlying = self._by_id.get(fc.component_id)
                if underlying is None:
                    _LOGGER.warning(
                        "Board %s featured.%s references unknown component %s — skipping",
                        board.id,
                        fc.id,
                        fc.component_id,
                    )
                    continue
                self._featured_by_id[full_id] = _FeaturedRecord(
                    full_id=full_id,
                    board_id=board.id,
                    featured=fc,
                    underlying=underlying,
                )
                ids.append(full_id)
            if ids:
                self._featured_by_board[board.id] = ids

    def _categories_for_board(
        self,
        board_id: str | None,
        *,
        query: str | None = None,
        exclude_set: set[str] | None = None,
        target_platform: str | None = None,
    ) -> list[dict[str, str | int]]:
        """
        Return the catalog category list, sorted by count desc then name.

        Each entry is a ``{id, name, count}`` dict. With no kwargs
        the counts cover the full catalog. Pass any of ``query`` /
        ``exclude_set`` / ``target_platform`` to apply the same
        filters used by :meth:`get_components`; categories whose
        post-filter count is zero are omitted. ``board_id`` adds
        the synthetic ``featured`` entry when the board has
        matching recommendations.
        """
        query_lower = query.lower() if query else None
        counts: dict[str, int] = {}
        for comp in self._components:
            if exclude_set is not None and comp.category in exclude_set:
                continue
            if (
                target_platform
                and comp.supported_platforms
                and target_platform not in comp.supported_platforms
            ):
                continue
            if query_lower and not (
                query_lower in comp.name.lower()
                or query_lower in comp.description.lower()
                or query_lower in comp.id.lower()
            ):
                continue
            counts[comp.category] = counts.get(comp.category, 0) + 1
        if board_id:
            # Featured rides on the same query so the badge drops to
            # the matches (or vanishes) while the user is searching.
            if query_lower is not None:
                featured_count = len(
                    self._featured_components_for_board(board_id, target_platform, query)
                )
            else:
                featured_count = len(self._featured_by_board.get(board_id, []))
            if featured_count:
                counts[ComponentCategory.FEATURED.value] = featured_count
        return sorted(
            [
                {"id": str(cat), "name": str(cat).replace("_", " ").title(), "count": count}
                for cat, count in counts.items()
            ],
            key=lambda c: (-int(c["count"]), str(c["name"])),
        )

    def _featured_components_for_board(
        self,
        board_id: str,
        target_platform: str | None,
        query: str | None,
    ) -> list[ComponentCatalogEntry]:
        """Materialise every featured component on *board_id*, optionally filtered by *query*."""
        ids = self._featured_by_board.get(board_id, [])
        entries: list[ComponentCatalogEntry] = []
        for full_id in ids:
            record = self._featured_by_id.get(full_id)
            if record is None:
                continue
            entries.append(_materialise_featured(record, target_platform))
        if query:
            query_lower = query.lower()
            entries = [
                e
                for e in entries
                if query_lower in e.name.lower()
                or query_lower in e.description.lower()
                or query_lower in e.id.lower()
            ]
        return entries

    def _resolve_platform(
        self,
        platform: str | None,
        board_id: str | None,
    ) -> str | None:
        """Normalise ``platform`` / derive it from ``board_id`` if needed.

        Lower-cases the platform string so frontend-supplied values
        like ``"ESP32"`` still match the catalog's lower-case
        ``supported_platforms`` entries. When only ``board_id`` is
        provided, look up the board to find its platform.
        """
        if platform:
            return platform.lower()
        if not board_id or self._db is None or self._db.boards is None:
            return None
        board = self._db.boards.get_by_id(board_id)
        if board is None or board.esphome.platform is None:
            return None
        return board.esphome.platform.value.lower()


# ---------------------------------------------------------------------------
# Featured registry
# ---------------------------------------------------------------------------


@dataclass
class _FeaturedRecord:
    """
    A featured component resolved against the underlying catalog entry.

    ``underlying`` is the regular catalog entry the user is actually
    adding (``switch.gpio``, ...); ``featured`` carries the manifest's
    name/description overrides and per-field presets to layer on top.
    """

    full_id: str
    board_id: str
    featured: FeaturedComponent
    underlying: ComponentCatalogEntry

    @property
    def underlying_id(self) -> str:
        return self.underlying.id


def _materialise_featured(
    record: _FeaturedRecord,
    target_platform: str | None,
) -> ComponentCatalogEntry:
    """
    Return *record* as a ``ComponentCatalogEntry`` ready for the catalog API.

    The result carries the synthetic ``featured.<board>.<local>`` id and
    category ``featured``, the manifest's name/description overrides, and
    each ``FieldPreset`` baked into the corresponding ``ConfigEntry`` as
    ``default_value`` / ``locked`` / ``suggestions``.
    """
    underlying = record.underlying
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
    )


# ---------------------------------------------------------------------------
# JSON → model loaders
# ---------------------------------------------------------------------------


def _safe_enum(enum_cls: type, value: Any, default: Any | None = None) -> Any:
    """Coerce *value* to an enum member, returning *default* on failure."""
    if value is None or value == "":
        return default
    try:
        return enum_cls(value)
    except (ValueError, KeyError):
        return default


def _load_pin_features(raw: Any) -> list[PinFeature]:
    """Parse a list of pin-feature strings, dropping unknown values."""
    if not isinstance(raw, list):
        return []
    out: list[PinFeature] = []
    for item in raw:
        feat = _safe_enum(PinFeature, item)
        if feat is not None:
            out.append(feat)
    return out


def _load_unit_options(raw: Any) -> list[str] | None:
    """Normalise the JSON ``unit_options`` field into a list of strings.

    ``None`` for non-FLOAT_WITH_UNIT entries (the catalog omits the
    field entirely on those). Non-list / empty values fold back to
    ``None`` so a malformed catalog entry doesn't reach the frontend
    as a half-populated picker — same shape as ``_load_options``.
    """
    if not isinstance(raw, list) or not raw:
        return None
    out = [str(item) for item in raw if isinstance(item, str)]
    return out or None


def _load_options(raw: Any) -> list[ConfigValueOption] | None:
    """
    Normalise the JSON ``options`` field into ConfigValueOption objects.

    Accepts either a list of plain strings (each used as both label and
    value) or a list of ``{label, value}`` dicts.
    """
    if not isinstance(raw, list) or not raw:
        return None
    out: list[ConfigValueOption] = []
    for item in raw:
        if isinstance(item, str):
            out.append(ConfigValueOption(label=item, value=item))
        elif isinstance(item, dict):
            value = str(item.get("value", ""))
            label = str(item.get("label", value))
            out.append(ConfigValueOption(label=label, value=value))
    return out or None


def _load_display_format(raw: Any) -> str | None:
    """
    Normalise the JSON ``display_format`` field.

    Currently only ``"hex"`` is recognised; anything else (an unknown
    future variant a stale frontend wouldn't understand, garbage in
    the catalog, the common ``None`` for non-hex fields) folds back
    to ``None`` so the frontend's renderer falls through to the
    decimal-number default. Mirrors the ``_safe_enum`` policy used
    for ``pin_mode`` etc. — the catalog can introduce new variants
    without breaking dashboards still on an older release.
    """
    if raw == "hex":
        return "hex"
    return None


def _load_config_entry(data: dict) -> ConfigEntry:
    """Load a ConfigEntry from its JSON representation."""
    range_val: tuple[int | float, int | float] | None = None
    raw_range = data.get("range")
    if isinstance(raw_range, (list, tuple)) and len(raw_range) == 2:
        range_val = (raw_range[0], raw_range[1])

    nested_raw = data.get("config_entries")
    nested = (
        [_load_config_entry(e) for e in nested_raw]
        if isinstance(nested_raw, list) and nested_raw
        else None
    )

    return ConfigEntry(
        key=data["key"],
        type=_safe_enum(ConfigEntryType, data.get("type"), ConfigEntryType.UNKNOWN),
        label=data.get("label") or data["key"],
        description=data.get("description"),
        required=bool(data.get("required", False)),
        default_value=data.get("default_value"),
        platform_defaults=data.get("platform_defaults"),
        options=_load_options(data.get("options")),
        allow_custom_value=bool(data.get("allow_custom_value", False)),
        range=range_val,
        display_format=_load_display_format(data.get("display_format")),
        unit_options=_load_unit_options(data.get("unit_options")),
        multi_value=bool(data.get("multi_value", False)),
        templatable=bool(data.get("templatable", False)),
        depends_on=data.get("depends_on"),
        depends_on_value=data.get("depends_on_value"),
        depends_on_value_not=data.get("depends_on_value_not"),
        depends_on_component=data.get("depends_on_component"),
        references_component=data.get("references_component"),
        pin_features=_load_pin_features(data.get("pin_features")),
        pin_mode=_safe_enum(PinMode, data.get("pin_mode")),
        advanced=bool(data.get("advanced", False)),
        hidden=bool(data.get("hidden", False)),
        help_link=data.get("help_link"),
        translation_key=data.get("translation_key"),
        translation_params=data.get("translation_params"),
        config_entries=nested,
        platform_type=data.get("platform_type") or None,
    )


def _load_component(data: dict) -> ComponentCatalogEntry:
    """Load a ComponentCatalogEntry from its JSON representation."""
    return ComponentCatalogEntry(
        id=data["id"],
        name=data.get("name", data["id"]),
        description=data.get("description", ""),
        category=_safe_enum(ComponentCategory, data.get("category"), ComponentCategory.MISC),
        docs_url=data.get("docs_url", ""),
        image_url=data.get("image_url", ""),
        dependencies=list(data.get("dependencies", [])),
        multi_conf=bool(data.get("multi_conf", False)),
        supported_platforms=list(data.get("supported_platforms", [])),
        config_entries=[_load_config_entry(e) for e in data.get("config_entries", [])],
    )
