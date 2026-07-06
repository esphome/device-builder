"""Component catalog controller."""

from __future__ import annotations

import logging
import re
from functools import partial
from typing import TYPE_CHECKING, Any

from ...definitions import (
    load_featured_components_index,
    load_pin_registry_modes_index,
    load_platform_capabilities_index,
)
from ...helpers.api import api_command
from ...helpers.device_yaml import NETWORK_PROVIDER_COMPONENT_IDS
from ...helpers.json import loads
from ...helpers.lazy_catalog import LazyBodyStore
from ...models import (
    ComponentCatalogEntry,
    ComponentCatalogIndexEntry,
    ComponentCategory,
    IntegrationDocEntry,
    PagedComponentsResponse,
)
from ...models.boards import normalize_platform
from ..devices.helpers import _apply_featured_presets
from ._resolve import (
    _BODY_CACHE_MAXSIZE,
    _COMPONENTS_INDEX_JSON,
    _FEATURED_PREFIX,
    INTERNAL_COMPONENT_IDS,
    _as_category_set,
    _FeaturedRecord,
    _load_body_from_disk,
    _materialise,
    _materialise_featured,
    _materialise_featured_index,
)

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...models import BoardCatalogEntry

_LOGGER = logging.getLogger(__name__)


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_EMPHASIS_RE = re.compile(r"[*_`]")
_FIRST_SENTENCE_RE = re.compile(r"(.{1,240}?\.)(?:\s|$)")


def _summarize_description(text: str) -> str:
    """First sentence of *text* with markdown flattened, capped at 240 chars."""
    flat = " ".join(_MD_EMPHASIS_RE.sub("", _MD_LINK_RE.sub(r"\1", text)).split())
    match = _FIRST_SENTENCE_RE.match(flat)
    return match.group(1) if match else flat[:240]


def variant_to_key(variant: str) -> str:
    """Normalise an ``Esp32Variant`` value (``esp32c3``) to a catalog key (``esp32_c3``).

    Matches the ``platform_defaults`` / ``platform_options`` key form; the
    base ``esp32`` and non-ESP32 platforms pass through unchanged. Shared with
    ``script/sync_components.py`` (re-exported via the package) so build-time
    keys and runtime lookups can't diverge.
    """
    low = variant.lower()
    if low.startswith("esp32") and low != "esp32":
        return "esp32_" + low[len("esp32") :]
    return low


class ComponentCatalog:
    """In-memory component catalog with search and pagination."""

    def __init__(self, device_builder: DeviceBuilder | None = None) -> None:
        self._db = device_builder
        # Slim index — loaded eagerly. Bodies live in per-id files on
        # disk and hydrate on demand through ``_body_store``.
        self._components: list[ComponentCatalogIndexEntry] = []
        self._by_id: dict[str, ComponentCatalogIndexEntry] = {}
        # Featured-component lookups, populated by ``_build_featured_registry``
        # after both catalogs have loaded. The ``_by_board`` index is what
        # lets ``get_components`` scope a ``category=featured`` query to one
        # board's recommendations rather than the whole catalog.
        self._featured_by_id: dict[str, _FeaturedRecord] = {}
        self._featured_by_board: dict[str, list[str]] = {}
        # ``{provider_key: [allowed_mode_flags]}`` — lets the frontend scope
        # the long-form pin Mode checkboxes for external pin providers (an
        # expander like pca9554 allows only input/output). Loaded in ``load()``;
        # empty (or a native pin with no provider key) leaves the frontend
        # showing every flag (the pre-scoping behaviour).
        self._pin_registry_modes: dict[str, list[str]] = {}
        self._body_store: LazyBodyStore[ComponentCatalogEntry] = LazyBodyStore(
            load_one=_load_body_from_disk,
            cache_maxsize=_BODY_CACHE_MAXSIZE,
            is_known=lambda cid: cid in self._by_id,
        )

    def load(self) -> None:
        """
        Load the slim component index from disk.

        Logs a warning and leaves the catalog empty when the index is
        missing — run ``script/sync_components.py`` to (re)generate
        it. Bodies (``definitions/components/<id>.json``) load on
        demand through :meth:`get_body`.
        """
        if not _COMPONENTS_INDEX_JSON.exists():
            _LOGGER.warning(
                "Component index not found at %s — run script/sync_components.py",
                _COMPONENTS_INDEX_JSON,
            )
            return

        # ``loads`` (orjson) decodes UTF-8 bytes directly — faster than
        # stdlib json and dodges the platform-locale-encoding trap that
        # bit Windows on read_text without an explicit encoding.
        data = loads(_COMPONENTS_INDEX_JSON.read_bytes())
        # Drop ESPHome internal-helper / auto-load-target components
        # — see ``INTERNAL_COMPONENT_IDS`` for the why.
        self._components = [
            ComponentCatalogIndexEntry.from_dict(c)
            for c in data.get("components", [])
            if c.get("id") not in INTERNAL_COMPONENT_IDS
        ]
        self._by_id = {c.id: c for c in self._components}
        self._build_featured_registry()
        self._pin_registry_modes = load_pin_registry_modes_index()
        _LOGGER.info(
            "Component catalog loaded: %d components (slim index), %d featured, %d pin registries",
            len(self._components),
            len(self._featured_by_id),
            len(self._pin_registry_modes),
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

    @api_command("components/get_pin_registry_modes")
    async def get_pin_registry_modes(self, **kwargs: Any) -> dict[str, list[str]]:
        """
        Return ``{provider_key: [allowed_mode_flags]}`` for pin Mode scoping.

        The long-form pin Mode checkboxes an external pin provider supports are
        a subset of the five flags (an I2C expander like ``pca9554`` allows only
        input/output), keyed on the provider key that appears in the pin value.
        Native pins (no provider key) and a missing artefact both fall back to
        every flag.
        """
        return self._pin_registry_modes

    @api_command("components/get_integration_docs")
    async def get_integration_docs(self, **kwargs: Any) -> dict[str, IntegrationDocEntry]:
        """Return ``{integration_name: {url, name, description}}`` per integration.

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
        4. Qualified alias (``esphome.ota`` / ``ota.esphome`` →
           ``ota.esphome``'s docs URL): both orders of every dotted id,
           for platform log tags, whose order is inconsistent upstream.
        5. Derived helper alias (``esp32_ble_client`` → ``esp32_ble``'s
           page): a component in the sync-time ``component_names`` snapshot
           with no page of its own inherits its longest documented
           multi-word name prefix.

        Names with no catalog hit are simply omitted — the frontend
        renders them as plain text. The catalog's ``docs_url`` is sourced
        from the live esphome.io docs index, so a present URL is also a
        guarantee that the page exists. ``name`` is the catalog display
        name (``ethernet`` → "Ethernet Component") and ``description`` its
        first sentence; category landings, which have no catalog entry,
        fall back to the bare key and an empty description.
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
        top_level: dict[str, IntegrationDocEntry] = {}
        category_urls: dict[str, IntegrationDocEntry] = {}
        stem_candidates: dict[str, dict[str, IntegrationDocEntry]] = {}
        qualified: list[tuple[str, IntegrationDocEntry]] = []
        for comp in self._components:
            comp_id = comp.id
            docs = comp.docs_url
            if not comp_id or not docs:
                continue
            entry = IntegrationDocEntry(
                url=docs,
                name=comp.name,
                description=_summarize_description(comp.description),
            )
            if "." not in comp_id:
                top_level[comp_id] = entry
                continue
            qualified.append((comp_id, entry))
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
                category_urls.setdefault(
                    category,
                    IntegrationDocEntry(
                        url=docs[: idx + len(marker) - 1], name=category, description=""
                    ),
                )
            stem_candidates.setdefault(stem, {})[docs] = entry

        # Stems are unambiguous only when every category that owns the
        # stem agrees on the same docs URL. Multi-platform components
        # (``at581x``, ``rotary_encoder``) hit this path because they
        # share a single docs page across categories.
        stems: dict[str, IntegrationDocEntry] = {
            stem: next(iter(by_url.values()))
            for stem, by_url in stem_candidates.items()
            if len(by_url) == 1
        }

        # ``dict.update()`` overwrites existing keys, so later writes
        # win. Apply lowest priority first (stems), then category, then
        # top-level — that way a colliding key is overridden by the
        # more-specific page.
        result: dict[str, IntegrationDocEntry] = {}
        result.update(stems)
        result.update(category_urls)
        result.update(top_level)
        self._add_docs_aliases(
            result, qualified, load_platform_capabilities_index().component_names
        )
        return result

    async def get_component(
        self,
        *,
        component_id: str,
        platform: str | None = None,
        board_id: str | None = None,
    ) -> ComponentCatalogEntry | None:
        """
        Resolve one component id; thin wrapper around the batch API.

        Not a WS command — the frontend always batches through
        ``components/get_component_bodies``. Kept as a sync-call
        convenience for internal callers and tests so the
        per-id-lookup story doesn't fork.
        """
        bodies = await self.get_component_bodies(
            component_ids=[component_id],
            platform=platform,
            board_id=board_id,
        )
        return bodies.get(component_id)

    @api_command("components/get_component_bodies")
    async def get_component_bodies(
        self,
        *,
        component_ids: list[str],
        platform: str | None = None,
        board_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, ComponentCatalogEntry]:
        """
        Hydrate a batch of component bodies in one round trip.

        Returns a dict keyed by the requested id; missing / unknown
        ids are omitted. Duplicate ids collapse to one entry.
        ``component_ids`` may include ``featured.<board>.<local>``
        synthetic ids; their underlying bodies are loaded
        transparently.

        ``platform`` / ``board_id`` resolve ``platform_defaults``
        into ``default_value`` uniformly across every returned
        entry. For featured ids, the explicit ``platform`` wins and
        ``board_id`` falls back to the record's own board so the
        right per-board defaults land.
        """
        unique_ids = list(dict.fromkeys(component_ids))
        # Collect every underlying body the batch touches and load
        # them in one executor hop; the per-id materialise pass
        # below reads from the returned dict, not the cache, so
        # batches larger than ``_BODY_CACHE_MAXSIZE`` don't lose
        # their own early entries to eviction.
        underlying_ids = [
            uid for cid in unique_ids if (uid := self._underlying_id(cid)) is not None
        ]
        bodies = await self._load_bodies(underlying_ids)
        return {
            cid: entry
            for cid in unique_ids
            if (
                entry := self._resolve_one_from_bodies(
                    cid, bodies, platform=platform, board_id=board_id
                )
            )
            is not None
        }

    @api_command("components/get_components")
    async def get_components(
        self,
        *,
        query: str | None = None,
        category: ComponentCategory | str | list[str] | None = None,
        exclude_category: ComponentCategory | str | list[str] | None = None,
        platform: str | None = None,
        board_id: str | None = None,
        provides: str | None = None,
        offset: int = 0,
        limit: int = 50,
        **kwargs: Any,
    ) -> PagedComponentsResponse:
        """
        Get components with optional filtering, search, and pagination.

        ``query`` matches against the component id, name, and description.
        ``platform`` filters to components compatible with the given
        target platform — components with an empty ``supported_platforms``
        list are considered platform-agnostic and always included.

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

        ``provides`` filters to components that can be referenced *as*
        the given interface (e.g. ``provides="voltage_sampler"`` returns
        the ADC-family sensors), so the Add-component picker can offer
        valid targets for a cross-domain ``references_component`` field.

        Featured components lead the results whenever ``board_id`` is
        set and the filter doesn't scope to a specific non-``featured``
        category — so the unfiltered "All" listing returns them first,
        then the regular entries. A specific ``category`` (e.g.
        ``"sensor"``) drops them unless it names ``featured``; an
        explicit ``exclude_category=["featured"]`` always wins.

        Response entries are the slim :class:`ComponentCatalogIndexEntry`
        shape; the per-field ``config_entries`` tree is fetched on
        demand via ``components/get_component_bodies`` when the user
        opens a card.
        """
        target_platform = self._resolve_platform(platform, board_id)
        include_set = _as_category_set(category) if category else None
        exclude_set = _as_category_set(exclude_category) if exclude_category else None

        # Board id when featured entries apply, else None (one ``is not None``
        # test, and mypy narrows it for the call below). Featured lead the
        # results whenever a board is set and the filter isn't scoped to a
        # specific non-featured category; the "All" listing (no category)
        # includes them too, and an explicit exclude of ``featured`` always wins.
        featured_board = (
            board_id
            if board_id is not None
            and (include_set is None or ComponentCategory.FEATURED.value in include_set)
            and (exclude_set is None or ComponentCategory.FEATURED.value not in exclude_set)
            else None
        )
        featured_entries = (
            self._featured_components_for_board(featured_board, query)
            if featured_board is not None
            else []
        )
        # ``provides`` narrows both result sets so a featured-category query
        # stays consistent with the regular catalog.
        if provides:
            featured_entries = [c for c in featured_entries if provides in c.provides]

        # Featured entries live in their own registry, never in
        # ``self._components``; strip the synthetic category before applying
        # the include filter so it doesn't filter out every regular entry.
        regular_include = (
            include_set - {ComponentCategory.FEATURED.value} if include_set is not None else None
        )

        if include_set is not None and not regular_include:
            results: list[ComponentCatalogIndexEntry] = []
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
            if provides:
                results = [c for c in results if provides in c.provides]
            if query:
                query_lower = query.lower()
                results = [
                    c
                    for c in results
                    if query_lower in c.name.lower()
                    or query_lower in c.description.lower()
                    or query_lower in c.id.lower()
                ]
                # Rank exact id/name matches first so e.g. searching "uart"
                # surfaces UART Bus above components that only mention it in
                # their description. Stable sort keeps catalog order per rank.
                results = sorted(results, key=partial(_query_rank, query_lower=query_lower))

        total_featured = len(featured_entries)
        total = total_featured + len(results)
        end = offset + limit
        page: list[ComponentCatalogIndexEntry] = []
        if offset < total_featured:
            page.extend(featured_entries[offset : min(end, total_featured)])
        regular_start = max(0, offset - total_featured)
        regular_end = max(0, end - total_featured)
        if regular_end > regular_start:
            page.extend(results[regular_start:regular_end])

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

    def index_title(self, component_id: str) -> str | None:
        """Catalog title for *component_id* from the in-RAM slim index, or ``None``."""
        entry = self._by_id.get(component_id)
        return entry.name if entry else None

    async def get_body(self, component_id: str) -> ComponentCatalogEntry | None:
        """Return the hydrated body for *component_id*, or ``None`` if missing."""
        return await self._body_store.get(component_id)

    async def _load_bodies(self, component_ids: list[str]) -> dict[str, ComponentCatalogEntry]:
        """Batched variant of :meth:`get_body`; one executor hop per call."""
        return await self._body_store.get_many(component_ids)

    def _add_docs_aliases(
        self,
        result: dict[str, IntegrationDocEntry],
        qualified: list[tuple[str, IntegrationDocEntry]],
        component_names: list[str],
    ) -> None:
        """
        Add log-tag alias keys to the map in place.

        Both orders of every qualified id (upstream tag order is
        inconsistent); id-exact keys land before reversed aliases, then
        undocumented ``component_names`` inherit their family's page.
        """
        for comp_id, entry in qualified:
            result.setdefault(comp_id, entry)
        for comp_id, entry in qualified:
            category, stem = comp_id.split(".", 1)
            result.setdefault(f"{stem}.{category}", entry)

        # Undocumented internal helpers (esp32_ble_client, web_server_base)
        # inherit the docs page of their longest documented name prefix.
        # The prefix must itself stay multi-word — a one-word prefix hit
        # (``sensor``, ``time``) is coincidence, not component family.
        for name in component_names:
            if name in result:
                continue
            prefix = name
            while "_" in (prefix := prefix.rsplit("_", 1)[0]):
                family = result.get(prefix)
                if family:
                    result[name] = family
                    break

    def get_featured_record(self, component_id: str) -> _FeaturedRecord | None:
        """Return the registry record for a ``featured.*`` id, or ``None``."""
        return self._featured_by_id.get(component_id)

    def _underlying_id(self, component_id: str) -> str | None:
        """
        Map a wire id to the catalog body it resolves to.

        Regular ids return unchanged. ``featured.<board>.<local>``
        ids return the underlying ``<domain>.<stem>`` id from the
        featured registry. Returns ``None`` when the featured id
        is unknown so callers can skip it cleanly.
        """
        if not component_id.startswith(_FEATURED_PREFIX):
            return component_id
        record = self._featured_by_id.get(component_id)
        return record.underlying_id if record is not None else None

    def _resolve_one_from_bodies(
        self,
        component_id: str,
        bodies: dict[str, ComponentCatalogEntry],
        *,
        platform: str | None,
        board_id: str | None,
    ) -> ComponentCatalogEntry | None:
        """
        Materialise one id from a pre-loaded body map.

        Pure dict lookup + platform resolution; no I/O. Returns
        ``None`` when the featured id is unknown or the underlying
        body wasn't loaded. For a featured id, explicit ``platform``
        wins and ``board_id`` falls back to the record's own board
        so ``platform_defaults`` resolve against the right target.
        """
        if component_id.startswith(_FEATURED_PREFIX):
            record = self._featured_by_id.get(component_id)
            if record is None:
                return None
            body = bodies.get(record.underlying_id)
            if body is None:
                return None
            target_platform = self._resolve_platform(platform, record.board_id)
            target_variant = self._resolve_variant(record.board_id)
            return _materialise_featured(record, body, target_platform, target_variant)
        body = bodies.get(component_id)
        if body is None:
            return None
        target_platform = self._resolve_platform(platform, board_id)
        target_variant = self._resolve_variant(board_id)
        return _materialise(body, target_platform, target_variant)

    async def resolve_default_components(
        self,
        board: BoardCatalogEntry,
    ) -> list[tuple[ComponentCatalogEntry, dict[str, Any]]]:
        """
        Resolve a board's ``default_components`` into ``(component, fields)`` pairs.

        Each entry's ``id`` is tried first as a local
        ``featured_components.id`` on the same board (picking up
        that entry's full field presets); falls through to a bare
        catalog ``component_id`` lookup. The entry's own ``fields``
        dict layers on top of any featured presets, with inline
        values winning. Unknown references are skipped with a
        warning — the manifest validator is the contract that
        keeps these from reaching runtime.
        """
        # Collect every underlying body the board's defaults touch
        # so we can load them in one executor hop, mirroring
        # ``get_component_bodies``. Pre-classifying each entry into
        # (record, underlying_id) avoids a second pass through the
        # featured registry below.
        targets: list[tuple[Any, _FeaturedRecord | None, str]] = []
        for entry in board.default_components:
            full_id = f"{_FEATURED_PREFIX}{board.id}.{entry.id}"
            record = self._featured_by_id.get(full_id)
            underlying_id = record.underlying_id if record is not None else entry.id
            targets.append((entry, record, underlying_id))
        bodies = await self._load_bodies([t[2] for t in targets])
        out: list[tuple[ComponentCatalogEntry, dict[str, Any]]] = []
        for entry, record, underlying_id in targets:
            body = bodies.get(underlying_id)
            if body is None:
                if record is not None:
                    _LOGGER.warning(
                        "Board %s default_components featured ref %s has no body — skipping",
                        board.id,
                        entry.id,
                    )
                else:
                    _LOGGER.warning(
                        "Board %s default_components references unknown id %s — skipping",
                        board.id,
                        entry.id,
                    )
                continue
            if record is not None:
                fields = _apply_featured_presets(record, {}, body)
                fields.update(entry.fields)
                out.append((body, fields))
            else:
                out.append((body, dict(entry.fields)))
        return out

    async def resolve_network_components(
        self,
        board: BoardCatalogEntry,
    ) -> list[tuple[ComponentCatalogEntry, dict[str, Any]]]:
        """
        Resolve a board's onboard-network suggested hardware to a ``(component, fields)`` pair.

        Networking is a single decision, so the first
        ``featured_components`` entry whose ``component_id`` is in
        ``NETWORK_PROVIDER_COMPONENT_IDS`` (``ethernet``) wins — its locked
        presets seed a wired device in place of Wi-Fi. Returned as a
        list (at most one) so the caller can ``extend`` *defaults*; empty
        when the board offers no provider.
        """
        for fc in board.featured_components:
            if fc.component_id not in NETWORK_PROVIDER_COMPONENT_IDS:
                continue
            record = self._featured_by_id.get(f"{_FEATURED_PREFIX}{board.id}.{fc.id}")
            if record is None:
                continue
            bodies = await self._load_bodies([record.underlying_id])
            body = bodies.get(record.underlying_id)
            if body is None:
                _LOGGER.warning(
                    "Board %s network featured %s has no body — skipping", board.id, fc.id
                )
                continue
            return [(body, _apply_featured_presets(record, {}, body))]
        return []

    def _build_featured_registry(self) -> None:
        """Index every featured component from the precomputed map.

        Reads ``definitions/featured_components.index.json`` directly
        rather than walking per-board bodies — the index carries
        every ``FeaturedComponent`` aggregated by board id, so the
        registry build pays zero board-body loads at startup.
        """
        self._featured_by_id = {}
        self._featured_by_board = {}
        for board_id, featured in load_featured_components_index().items():
            ids: list[str] = []
            for fc in featured:
                full_id = f"{_FEATURED_PREFIX}{board_id}.{fc.id}"
                underlying = self._by_id.get(fc.component_id)
                if underlying is None:
                    _LOGGER.warning(
                        "Board %s featured.%s references unknown component %s — skipping",
                        board_id,
                        fc.id,
                        fc.component_id,
                    )
                    continue
                self._featured_by_id[full_id] = _FeaturedRecord(
                    full_id=full_id,
                    board_id=board_id,
                    featured=fc,
                    underlying_id=underlying.id,
                )
                ids.append(full_id)
            if ids:
                self._featured_by_board[board_id] = ids

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
        # Honor an explicit exclude of featured, mirroring the results path, so
        # the sidebar badge doesn't advertise recommendations the list drops.
        featured_excluded = (
            exclude_set is not None and ComponentCategory.FEATURED.value in exclude_set
        )
        if board_id and not featured_excluded:
            # Featured rides on the same query so the badge drops to
            # the matches (or vanishes) while the user is searching.
            if query_lower is not None:
                featured_count = len(self._featured_components_for_board(board_id, query))
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
        query: str | None,
    ) -> list[ComponentCatalogIndexEntry]:
        """Slim featured-card list for *board_id*, optionally filtered by *query*."""
        ids = self._featured_by_board.get(board_id, [])
        entries: list[ComponentCatalogIndexEntry] = []
        for full_id in ids:
            record = self._featured_by_id.get(full_id)
            if record is None:
                continue
            underlying = self._by_id.get(record.underlying_id)
            if underlying is None:
                continue
            entries.append(_materialise_featured_index(record, underlying))
        if query:
            query_lower = query.lower()
            entries = [
                e
                for e in entries
                if query_lower in e.name.lower()
                or query_lower in e.description.lower()
                or query_lower in e.id.lower()
            ]
            # Rank like the regular results so an exact hit leads the featured
            # cards too, instead of staying in curated order.
            entries = sorted(entries, key=partial(_query_rank, query_lower=query_lower))
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
            return normalize_platform(platform.lower())
        if not board_id or self._db is None or self._db.boards is None:
            return None
        board = self._db.boards.get_by_id(board_id)
        if board is None or board.esphome.platform is None:
            return None
        return board.esphome.platform.value.lower()

    def _resolve_variant(self, board_id: str | None) -> str | None:
        """Chip-variant key (``esp32_c3``) for *board_id*, else None.

        Resolves variant-specific ``platform_options`` / ``platform_defaults``;
        only ESP32 boards carry a variant, and the lookup falls back to the
        base platform when this is None.
        """
        if board_id is None or self._db is None or self._db.boards is None:
            return None
        board = self._db.boards.get_by_id(board_id)
        if board is None or board.esphome.variant is None:
            return None
        return variant_to_key(board.esphome.variant.value)


def _query_rank(entry: ComponentCatalogIndexEntry, query_lower: str) -> int:
    """Search relevance rank for *entry* against *query_lower*; lower sorts first."""
    name = entry.name.lower()
    cid = entry.id.lower()
    if query_lower in (cid, name):
        return 0  # exact id / name
    if cid.startswith(query_lower) or name.startswith(query_lower):
        return 1
    if query_lower in name:
        return 2
    if query_lower in cid:
        return 3
    return 4  # description-only match
