"""Component catalog controller."""

from __future__ import annotations

import logging
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
    PagedComponentsResponse,
    PinFeature,
    PinMode,
)

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)

_COMPONENTS_JSON = Path(__file__).resolve().parent.parent / "definitions" / "components.json"


class ComponentCatalog:
    """In-memory component catalog with search and pagination."""

    def __init__(self, device_builder: DeviceBuilder | None = None) -> None:
        self._db = device_builder
        self._components: list[ComponentCatalogEntry] = []
        self._by_id: dict[str, ComponentCatalogEntry] = {}

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
        self._components = [_load_component(c) for c in data.get("components", [])]
        self._by_id = {c.id: c for c in self._components}
        _LOGGER.info("Component catalog loaded: %d components", len(self._components))

    @property
    def categories(self) -> list[dict[str, str | int]]:
        """
        Return all component categories sorted by count (highest first).

        Each entry is a ``{id, name, count}`` dict suitable for direct
        use in the catalog UI's filter list.
        """
        counts: dict[str, int] = {}
        for comp in self._components:
            counts[comp.category] = counts.get(comp.category, 0) + 1
        return sorted(
            [
                {"id": str(cat), "name": str(cat).replace("_", " ").title(), "count": count}
                for cat, count in counts.items()
            ],
            key=lambda c: (-int(c["count"]), str(c["name"])),
        )

    @api_command("components/get_categories")
    async def get_categories(self, **kwargs: Any) -> list[dict[str, str | int]]:
        """Get all component categories with counts."""
        return self.categories

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
        """
        platform = self._resolve_platform(platform, board_id)
        component = self._by_id.get(component_id)
        if component is None:
            return None
        return _materialise(component, platform)

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
        """
        platform = self._resolve_platform(platform, board_id)
        results = self._components

        if category:
            include_set = _as_category_set(category)
            results = [c for c in results if c.category in include_set]

        if exclude_category:
            exclude_set = _as_category_set(exclude_category)
            results = [c for c in results if c.category not in exclude_set]

        if platform:
            results = [
                c for c in results if not c.supported_platforms or platform in c.supported_platforms
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

        total = len(results)
        page = [_materialise(c, platform) for c in results[offset : offset + limit]]
        return PagedComponentsResponse(
            components=page,
            total=total,
            offset=offset,
            limit=limit,
            categories=self.categories,
        )

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
