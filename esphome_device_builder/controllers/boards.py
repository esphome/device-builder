"""Board catalog controller."""

from __future__ import annotations

import logging
from typing import Any

from ..definitions import load_board_catalog
from ..helpers.api import api_command
from ..models import BoardCatalogEntry, BoardTag, Esp32Variant, PagedBoardsResponse, Platform

_LOGGER = logging.getLogger(__name__)


class BoardCatalog:
    """In-memory board catalog with search and pagination."""

    def __init__(self) -> None:
        self._boards: list[BoardCatalogEntry] = []

    def load(self) -> None:
        """Load boards from YAML definitions on disk."""
        catalog = load_board_catalog()
        self._boards = list(catalog.boards)
        _LOGGER.info("Board catalog loaded: %d boards", len(self._boards))

    @api_command("boards/get_board")
    async def get_board(self, *, board_id: str, **kwargs: Any) -> BoardCatalogEntry | None:
        """Get a single board by ID."""
        for board in self._boards:
            if board.id == board_id:
                return board
        return None

    @api_command("boards/get_boards")
    async def get_boards(
        self,
        *,
        query: str | None = None,
        platform: Platform | str | None = None,
        variant: Esp32Variant | str | None = None,
        tag: BoardTag | str | None = None,
        offset: int = 0,
        limit: int = 50,
        **kwargs: Any,
    ) -> PagedBoardsResponse:
        """
        Get boards with optional filtering, search, and pagination.

        ``query`` matches the board id, name, manufacturer, description
        and tags. Featured boards are sorted first; generic fallback
        boards last; the rest alphabetically.
        """
        results = self._boards

        if platform:
            results = [b for b in results if b.esphome.platform == platform]

        if variant:
            variant_lower = variant.lower()
            results = [
                b
                for b in results
                if b.esphome.variant and b.esphome.variant.lower() == variant_lower
            ]

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

        results = sorted(
            results,
            key=lambda b: (not b.featured, b.is_generic, b.name.lower()),
        )

        total = len(results)
        page = results[offset : offset + limit]
        return PagedBoardsResponse(boards=page, total=total, offset=offset, limit=limit)

    def get_by_id(self, board_id: str) -> BoardCatalogEntry | None:
        """Look up a board by id synchronously. Returns ``None`` when not found."""
        for board in self._boards:
            if board.id == board_id:
                return board
        return None

    def iter_boards(self) -> list[BoardCatalogEntry]:
        """
        Return every loaded board.

        Returns the internal list directly — callers must treat it as
        read-only. Used by the components controller to build the
        featured-component registry at startup.
        """
        return self._boards

    def find_by_pio_board(self, pio_board: str, pio_variant: str = "") -> BoardCatalogEntry | None:
        """
        Find a board by its PlatformIO board id, preferring a matching variant.

        Used to derive a board_id from a user-provided YAML config.
        Returns None if no entry has a matching ``esphome.board`` value.

        When multiple catalog entries share the same PlatformIO board
        id (e.g. several products are physically built on the same
        ``esp32-c3-devkitm-1`` reference design), the disambiguation
        ladder is:

        1. ``is_generic=true`` wins outright — the catalog's curated
           "this is the canonical reference design" marker.
        2. Otherwise, prefer an entry whose ``id`` matches the
           PlatformIO board id (after ``_`` ↔ ``-`` normalization).
           A board with id ``d1-mini`` is the canonical entry for
           PlatformIO ``d1_mini`` even when nobody remembered to set
           ``is_generic: true`` — without this tiebreaker, a vendor
           product alphabetically earlier than the canonical entry
           wins (the bug behind issue #395 — AquaPing showing up as
           the board for plain ``d1_mini`` YAMLs).
        3. Fall back to the first match in iteration order.

        Mirrors the generic-preference policy in
        ``find_by_platform_variant``. The id-match tiebreaker is
        specific to ``find_by_pio_board`` because that function's
        input is itself a board id; the platform-only variant lookup
        has nothing comparable to match against.
        """
        matches = [b for b in self._boards if b.esphome.board == pio_board]
        if not matches:
            return None
        if pio_variant:
            variant_matches = [
                b for b in matches if b.esphome.variant and b.esphome.variant.value == pio_variant
            ]
            if variant_matches:
                matches = variant_matches
        for b in matches:
            if b.is_generic:
                return b
        # Tiebreaker: prefer the entry whose id equals the pio_board
        # id under ``_`` ↔ ``-`` normalization. Catalog ids tend to
        # use ``-`` while PlatformIO ids tend to use ``_``, and the
        # canonical entry for a given pio_board is the one named
        # after it.
        normalized_pio = pio_board.replace("_", "-")
        for b in matches:
            if b.id.replace("_", "-") == normalized_pio:
                return b
        return matches[0]

    def find_by_platform_variant(
        self,
        platform: str,
        variant: str = "",
    ) -> BoardCatalogEntry | None:
        """
        Find a board by ``platform`` (and optional ``variant``).

        Used as a final fallback when a YAML config names only the
        platform — common for users configuring a generic ``esp32:``
        block without a specific PlatformIO ``board:`` field. Generic
        catalog entries (``is_generic=true``) are preferred so the
        dashboard surfaces the right "Generic ESP32-C3" rather than a
        random vendor board that happens to share the same variant.
        """
        if not platform:
            return None
        matches = [
            b for b in self._boards if b.esphome.platform and b.esphome.platform.value == platform
        ]
        if not matches:
            return None
        if variant:
            variant_matches = [
                b for b in matches if b.esphome.variant and b.esphome.variant.value == variant
            ]
            if variant_matches:
                matches = variant_matches
        # Prefer the generic fallback so the dashboard tags untracked
        # YAML configs with a stable, well-known board.
        for b in matches:
            if b.is_generic:
                return b
        return matches[0]
