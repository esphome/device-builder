"""Board catalog controller — slim index + lazy bodies."""

from __future__ import annotations

import logging
from typing import Any

from ..definitions import (
    load_board_body_from_disk,
    load_board_index,
)
from ..helpers.api import api_command
from ..helpers.device_yaml import board_requires_wifi
from ..helpers.lazy_catalog import LazyBodyStore
from ..models import (
    BoardCatalogEntry,
    BoardCatalogIndex,
    BoardTag,
    Esp32Variant,
    PagedBoardsResponse,
    Platform,
)

_LOGGER = logging.getLogger(__name__)

# Bounded LRU for board bodies. Mirrors the components / automations
# catalogs (128). A typical session opens one board detail at a time;
# the wizard pre-fetch can blow past 128 but the cap exists so a
# misuse can't grow the heap unbounded.
_BODY_CACHE_MAXSIZE = 128


def _board_sort_key(board: BoardCatalogIndex) -> tuple[bool, bool, bool, str]:
    """Catalog display order: featured, generics, WiFi-capable within each, then by name."""
    return (
        not board.featured,
        not board.is_generic,
        BoardTag.WIFI not in board.tags,
        board.name.lower(),
    )


def _filter_by_variant(boards: list[BoardCatalogIndex], variant: str) -> list[BoardCatalogIndex]:
    """Boards whose variant equals *variant*, case-insensitively (empty if none)."""
    target = variant.lower()
    return [b for b in boards if b.esphome.variant and b.esphome.variant.value.lower() == target]


class BoardCatalog:
    """In-memory slim board index + lazy-loaded full bodies."""

    def __init__(self) -> None:
        self._boards: list[BoardCatalogIndex] = []
        self._known_ids: frozenset[str] = frozenset()
        self._body_store: LazyBodyStore[BoardCatalogEntry] = LazyBodyStore(
            load_one=load_board_body_from_disk,
            cache_maxsize=_BODY_CACHE_MAXSIZE,
            is_known=self._is_known,
        )

    def load(self) -> None:
        """Load the slim board index. Bodies hydrate on demand."""
        self._boards = load_board_index()
        self._known_ids = frozenset(b.id for b in self._boards)
        _LOGGER.info("Board catalog loaded: %d boards (slim index)", len(self._boards))

    def _is_known(self, board_id: str) -> bool:
        """Whether *board_id* exists in the slim index."""
        return board_id in self._known_ids

    @api_command("boards/get_board")
    async def get_board(self, *, board_id: str, **kwargs: Any) -> BoardCatalogEntry | None:
        """Get a single board's full body by id, or ``None`` if unknown."""
        board = await self._body_store.get(board_id)
        if board is not None:
            # Derived (not stored in the body JSON), idempotent on the cached
            # body: the wizard makes Wi-Fi mandatory for Wi-Fi-only boards and
            # skips the step for boards with their own network.
            board.requires_wifi = board_requires_wifi(board)
        return board

    @api_command("boards/get_boards")
    async def get_boards(
        self,
        *,
        query: str | None = None,
        platform: Platform | str | None = None,
        variant: Esp32Variant | str | None = None,
        mcu: str | None = None,
        tag: BoardTag | str | None = None,
        offset: int = 0,
        limit: int = 50,
        **kwargs: Any,
    ) -> PagedBoardsResponse:
        """
        Get boards with optional filtering, search, and pagination.

        ``query`` matches the board id, name, manufacturer, description
        and tags. ``mcu`` narrows the rp2040 platform to one chip series
        ("rp2040" / "rp2350"). Sort order: featured first; generics next;
        WiFi-capable boards ahead of non-WiFi within each group; then
        alphabetically. Returns slim :class:`BoardCatalogIndex` entries —
        the frontend's board detail view fetches full bodies via
        ``boards/get_board``.
        """
        results: list[BoardCatalogIndex] = self._boards

        if platform:
            results = [b for b in results if b.esphome.platform == platform]

        if variant:
            results = _filter_by_variant(results, variant)

        if mcu:
            mcu_lower = mcu.lower()
            results = [b for b in results if b.esphome.mcu and b.esphome.mcu.lower() == mcu_lower]

        if tag:
            tag_lower = tag.lower()
            results = [b for b in results if tag_lower in b.tags]

        if query:
            query_lower = query.lower()
            results = [
                b
                for b in results
                if query_lower in b.name.lower()
                or query_lower in b.description.lower()
                or query_lower in b.manufacturer.lower()
                or query_lower in b.id.lower()
                or any(query_lower in t for t in b.tags)
            ]

        results = sorted(results, key=_board_sort_key)

        total = len(results)
        page = results[offset : offset + limit]
        return PagedBoardsResponse(boards=page, total=total, offset=offset, limit=limit)

    @api_command("boards/get_compatible_boards")
    async def get_compatible_boards(self, *, board_id: str, **kwargs: Any) -> PagedBoardsResponse:
        """
        Boards interchangeable with ``board_id`` (same PlatformIO target).

        One page; includes ``board_id`` itself, empty when the id is unknown.
        """
        current = self.get_by_id(board_id)
        matches = (
            self.find_all_by_pio_board(current.esphome.board, current.esphome.platform)
            if current is not None
            else []
        )
        return PagedBoardsResponse(boards=matches, total=len(matches), offset=0, limit=len(matches))

    def get_by_id(self, board_id: str) -> BoardCatalogIndex | None:
        """Look up a slim board index entry by id, or ``None``."""
        for board in self._boards:
            if board.id == board_id:
                return board
        return None

    def _matches_pio_board(
        self,
        pio_board: str,
        platform: Platform | str | None = None,
    ) -> list[BoardCatalogIndex]:
        """Catalog entries on a PlatformIO board, optionally scoped to a platform."""
        matches = [b for b in self._boards if b.esphome.board == pio_board]
        if platform is not None:
            platform_value = platform.value if isinstance(platform, Platform) else platform
            matches = [b for b in matches if b.esphome.platform.value == platform_value]
        return matches

    def find_by_pio_board(
        self,
        pio_board: str,
        pio_variant: str = "",
        platform: Platform | str | None = None,
        *,
        prefer_exact_id: bool = False,
    ) -> BoardCatalogIndex | None:
        """
        Find a board by its PlatformIO board id, preferring a matching variant.

        Returns the slim index entry; the caller fetches the full body via
        :meth:`get_board` for pins / featured_components.

        ``platform`` scopes the match to one ESPHome platform — nRF52 and
        rp2040 both ship an ``adafruit_itsybitsy``, so an unscoped lookup
        would serve wrong-platform pins. A scoped miss returns ``None``.

        Several entries can share a PlatformIO board id (products built on one
        reference design). Tiebreak by *prefer_exact_id*:

        - ``False`` (default) — generic entry, else the entry whose ``id``
          matches the board id, else the first. The historical order; the
          ``board_id_user_set`` migration relies on it being unchanged.
        - ``True`` — exact ``id`` match first, else generic, else first. Used
          to resolve a device's own ``board:`` so ``board: esp01_1m`` lands on
          that entry, not the broader ``generic-esp8266`` sharing its pio board.
        """
        matches = self._matches_pio_board(pio_board, platform)
        if not matches:
            return None
        if pio_variant:
            matches = _filter_by_variant(matches, pio_variant) or matches
        normalized_pio = pio_board.replace("_", "-")
        id_match = next((b for b in matches if b.id.replace("_", "-") == normalized_pio), None)
        generic = next((b for b in matches if b.is_generic), None)
        ordered = (id_match, generic) if prefer_exact_id else (generic, id_match)
        return next((b for b in ordered if b is not None), matches[0])

    def find_all_by_pio_board(
        self,
        pio_board: str,
        platform: Platform | str | None = None,
    ) -> list[BoardCatalogIndex]:
        """All catalog entries on the same PlatformIO board, featured first then generics next."""
        return sorted(self._matches_pio_board(pio_board, platform), key=_board_sort_key)

    def find_by_platform_variant(
        self,
        platform: str,
        variant: str = "",
    ) -> BoardCatalogIndex | None:
        """
        Find a board by ``platform`` (and optional ``variant``), prefer generic.

        Returns the slim index entry — see :meth:`find_by_pio_board`
        for the body-fetch contract. Used as a final fallback when a
        YAML config names only the platform.
        """
        if not platform:
            return None
        matches = [b for b in self._boards if b.esphome.platform.value == platform]
        if not matches:
            return None
        if variant:
            matches = _filter_by_variant(matches, variant) or matches
        for b in matches:
            if b.is_generic:
                return b
        return matches[0]
