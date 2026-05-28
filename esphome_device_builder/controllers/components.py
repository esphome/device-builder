"""Component catalog controller."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..helpers.api import api_command
from ..helpers.json import loads
from ..models import (
    ComponentCatalogEntry,
    ComponentCatalogIndexEntry,
    ComponentCategory,
    ConfigEntry,
    ConfigEntryType,
    ConfigValueOption,
    FeaturedComponent,
    FieldPreset,
    PagedComponentsResponse,
    PinFeature,
    PinMode,
    RequiredGroup,
    RequiredGroupKind,
)
from .devices.helpers import _apply_featured_presets

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder
    from ..models import BoardCatalogEntry

# Prefix used to route featured-component IDs to the featured registry.
# Format: ``featured.<board_id>.<local_id>`` (e.g. ``featured.sonoff-basic.relay``).
_FEATURED_PREFIX = "featured."

_LOGGER = logging.getLogger(__name__)

_DEFINITIONS_DIR = Path(__file__).resolve().parent.parent / "definitions"
_COMPONENTS_INDEX_JSON = _DEFINITIONS_DIR / "components.index.json"
_COMPONENT_BODIES_DIR = _DEFINITIONS_DIR / "components"

# Bounded LRU for hydrated component bodies. The catalog ships ~900
# bodies totalling ~22MB on disk; pinning every loaded body would
# bring back the eager-load memory cost the split was meant to drop.
# Sized to absorb a navigator's full-device batch (typical
# ~30-50 components) plus headroom for featured-card warmups and
# yaml-completion lookups without thrashing.
_BODY_CACHE_MAXSIZE = 128

# LRU cap for ``is_unsafe_component_id`` (~5x the live catalog
# size). Bounded rather than ``@cache`` because the predicate
# sees external input (WS handler kwargs); unbounded growth on
# attacker-controlled ids would be a denial-of-service surface.
_UNSAFE_ID_CACHE_MAXSIZE = 4096

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
        # Slim index — loaded eagerly. Bodies live in per-id files on
        # disk and hydrate on demand through ``_body_cache``.
        self._components: list[ComponentCatalogIndexEntry] = []
        self._by_id: dict[str, ComponentCatalogIndexEntry] = {}
        # Featured-component lookups, populated by ``_build_featured_registry``
        # after both catalogs have loaded. The ``_by_board`` index is what
        # lets ``get_components`` scope a ``category=featured`` query to one
        # board's recommendations rather than the whole catalog.
        self._featured_by_id: dict[str, _FeaturedRecord] = {}
        self._featured_by_board: dict[str, list[str]] = {}
        self._body_cache: OrderedDict[str, ComponentCatalogEntry] = OrderedDict()
        # Single load lock + double-checked cache reads, mirroring
        # Home Assistant's ``translation.py``. The whole point is that
        # a batch of N ids runs as ONE executor job that reads every
        # missing body sequentially in the same thread, instead of N
        # ``asyncio.to_thread`` calls thrashing the thread pool for
        # small (<1ms) I/O. A concurrent fetch waiting on the lock
        # re-checks the cache after acquiring it, so same-id
        # coalescing falls out for free without per-id futures.
        self._body_load_lock = asyncio.Lock()

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
            _load_index_entry(c)
            for c in data.get("components", [])
            if c.get("id") not in INTERNAL_COMPONENT_IDS
        ]
        self._by_id = {c.id: c for c in self._components}
        self._build_featured_registry()
        _LOGGER.info(
            "Component catalog loaded: %d components (slim index), %d featured",
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

        Featured components are surfaced **only** when ``category``
        explicitly includes ``featured`` and ``board_id`` is set — the
        regular catalog listing never returns them. Mixed queries
        (e.g. ``category=["featured", "sensor"]``) return featured
        entries first followed by the matching regular entries.

        Response entries are the slim :class:`ComponentCatalogIndexEntry`
        shape; the per-field ``config_entries`` tree is fetched on
        demand via ``components/get_component_bodies`` when the user
        opens a card.
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
            self._featured_components_for_board(board_id, query)
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
            if query:
                query_lower = query.lower()
                results = [
                    c
                    for c in results
                    if query_lower in c.name.lower()
                    or query_lower in c.description.lower()
                    or query_lower in c.id.lower()
                ]

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

    async def get_body(self, component_id: str) -> ComponentCatalogEntry | None:
        """
        Return the hydrated body for *component_id*, or ``None`` if missing.

        Reads ``definitions/components/<id>.json`` on first access
        and caches up to ``_BODY_CACHE_MAXSIZE`` recent entries in
        an LRU. Concurrent calls for the same id (or for any id
        whose load is in flight) share one executor job thanks to
        the single load lock; the post-lock cache re-check fast-
        paths the second caller without a redundant disk read.
        Returns ``None`` when the id is absent from the index or
        its body file is missing on disk.
        """
        cached = self._body_cache.get(component_id)
        if cached is not None:
            self._body_cache.move_to_end(component_id)
            return cached
        if component_id not in self._by_id:
            return None
        bodies = await self._load_bodies([component_id])
        return bodies.get(component_id)

    async def _load_bodies(self, component_ids: list[str]) -> dict[str, ComponentCatalogEntry]:
        """
        Return hydrated bodies for *component_ids*; populates the cache as a side effect.

        One ``asyncio.to_thread`` for the whole batch so a request
        for N bodies pays one executor hop, not N. Held under
        :attr:`_body_load_lock` with a post-acquire cache re-check
        so an overlapping batch only loads the bodies the previous
        batch didn't already cover.

        Returns a dict of the entries that were loadable; unknown
        ids and missing-on-disk ids are absent. Callers must use
        the returned dict rather than re-reading the cache, because
        a batch larger than ``_BODY_CACHE_MAXSIZE`` would
        partially evict its own entries before returning. Cache is
        a hot-read optimization, not the correctness path.
        """
        async with self._body_load_lock:
            result: dict[str, ComponentCatalogEntry] = {}
            to_load: list[str] = []
            seen: set[str] = set()
            for cid in component_ids:
                # Dedupe inline so a caller passing the same id twice
                # (``resolve_default_components`` on a board that lists
                # the same underlying component under multiple
                # featured refs) doesn't make the executor job read
                # the same body file twice.
                if cid in seen or cid not in self._by_id:
                    continue
                seen.add(cid)
                cached = self._body_cache.get(cid)
                if cached is not None:
                    self._body_cache.move_to_end(cid)
                    result[cid] = cached
                else:
                    to_load.append(cid)
            if to_load:
                bodies = await asyncio.to_thread(_load_bodies_from_disk, to_load)
                for cid in to_load:
                    body = bodies.get(cid)
                    if body is None:
                        continue
                    self._body_cache[cid] = body
                    while len(self._body_cache) > _BODY_CACHE_MAXSIZE:
                        self._body_cache.popitem(last=False)
                    result[cid] = body
            return result

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
            return _materialise_featured(record, body, target_platform)
        body = bodies.get(component_id)
        if body is None:
            return None
        target_platform = self._resolve_platform(platform, board_id)
        return _materialise(body, target_platform)

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
                    underlying_id=underlying.id,
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


@cache
def _enum_value_map(enum_cls: type) -> dict[Any, Any]:
    """Memoised ``{member.value: member}`` map for an enum.

    Hot-path replacement for ``enum_cls(value)``; the stdlib's
    enum call walks every member on each lookup, which costs
    ~30% of catalog hydrate time across the 243k ``_safe_enum``
    calls a 100-component batch makes. One dict lookup beats the
    enum's per-call linear search.
    """
    return {m.value: m for m in enum_cls}  # type: ignore[attr-defined]


def _safe_enum(enum_cls: type, value: Any, default: Any | None = None) -> Any:
    """Coerce *value* to an enum member, returning *default* on failure."""
    if not value:
        return default
    return _enum_value_map(enum_cls).get(value, default)


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


def _load_required_groups(raw: Any) -> list[RequiredGroup]:
    """
    Normalise the JSON ``required_groups`` field into ``RequiredGroup`` objects.

    Returns an empty list for missing / malformed input so callers
    can store the field unconditionally. Unknown ``kind`` values
    (a future cardinality validator a stale dashboard wouldn't
    understand) drop the offending entry — same fail-soft policy
    as ``_load_display_format`` / ``_safe_enum``.
    """
    if not isinstance(raw, list) or not raw:
        return []
    out: list[RequiredGroup] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = _safe_enum(RequiredGroupKind, item.get("kind"))
        if kind is None:
            continue
        keys_raw = item.get("keys")
        if not isinstance(keys_raw, list):
            continue
        keys = [str(k) for k in keys_raw if isinstance(k, str)]
        if not keys:
            continue
        out.append(RequiredGroup(kind=kind, keys=keys))
    return out


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
        registry=data.get("registry"),
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
        supported_platforms=list(data.get("supported_platforms") or []),
        group=data.get("group") or None,
        required_groups=_load_required_groups(data.get("required_groups")),
    )


def _load_index_entry(data: dict) -> ComponentCatalogIndexEntry:
    """Load a ComponentCatalogIndexEntry from its JSON representation."""
    return ComponentCatalogIndexEntry(
        id=data["id"],
        name=data.get("name", data["id"]),
        description=data.get("description", ""),
        category=_safe_enum(ComponentCategory, data.get("category"), ComponentCategory.MISC),
        docs_url=data.get("docs_url", ""),
        image_url=data.get("image_url", ""),
        dependencies=list(data.get("dependencies", [])),
        multi_conf=bool(data.get("multi_conf", False)),
        supported_platforms=list(data.get("supported_platforms", [])),
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
        required_groups=_load_required_groups(data.get("required_groups")),
    )


def _load_body_from_disk(component_id: str) -> ComponentCatalogEntry | None:
    """Read ``components/<component_id>.json`` and hydrate into a ComponentCatalogEntry."""
    # Defense-in-depth path-traversal guard. ``component_id``
    # ultimately flows in from a WS handler kwarg; today the trust
    # chain is bounded because ``get_body`` short-circuits on
    # ``component_id not in self._by_id`` and the index is shipped
    # with the wheel, but a local reject-by-syntax check keeps the
    # safety property of the loader readable in isolation. The
    # check is purely on the id string (parent refs / separators /
    # null bytes) so the hot path stays out of the kernel ``lstat``
    # walk that ``Path.resolve`` does on every hydrate.
    if is_unsafe_component_id(component_id):
        _LOGGER.warning("Refusing component body for traversal-shaped id: %r", component_id)
        return None
    body_path = _COMPONENT_BODIES_DIR / f"{component_id}.json"
    if not body_path.is_file():
        _LOGGER.warning("Component body missing on disk: %s", body_path)
        return None
    return _load_component(loads(body_path.read_bytes()))


@lru_cache(maxsize=_UNSAFE_ID_CACHE_MAXSIZE)
def is_unsafe_component_id(component_id: str) -> bool:
    """
    Return True when *component_id* contains traversal-shaped characters.

    Shared by the runtime body loader (rejects + warns) and the
    sync script's body emitter (raises before write); both ends
    of the on-disk catalog stay narrow against the same predicate
    so a future bug on either side can't silently produce a path
    outside ``definitions/components/``. The result is cached
    because the same ~900 catalog ids repeat across every batch
    hydrate; the ``maxsize`` cap keeps an attacker-controlled
    flood from growing the cache without bound.
    """
    return (
        not component_id
        or ".." in component_id
        or "/" in component_id
        or "\\" in component_id
        or "\x00" in component_id
    )


def _load_bodies_from_disk(
    component_ids: list[str],
) -> dict[str, ComponentCatalogEntry | None]:
    """
    Read several component body files sequentially in one thread.

    Designed to be called from a single ``asyncio.to_thread``
    dispatch so a batch of N ids pays one executor hop instead of
    N. Missing files map to ``None``; the caller decides whether
    that's a cache miss or a hard error.
    """
    return {cid: _load_body_from_disk(cid) for cid in component_ids}
