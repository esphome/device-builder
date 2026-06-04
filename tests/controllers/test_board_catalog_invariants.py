"""Invariants over the real shipped board catalog.

Guards the catalog-data shape behind the stale board-card bug: a
device whose YAML names a PlatformIO board shared by several catalog
entries must resolve to a canonical (generic / id-matching) entry,
never an arbitrary vendor product.
"""

from __future__ import annotations

from collections import defaultdict

import pytest

from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.models import BoardCatalogIndex


@pytest.fixture(scope="module")
def real_catalog() -> BoardCatalog:
    """Load the real on-disk catalog once for the module."""
    cat = BoardCatalog()
    cat.load()
    return cat


def _is_canonical(entry: BoardCatalogIndex, pio_board: str) -> bool:
    """Whether *entry* is the canonical match for *pio_board*."""
    return entry.is_generic or entry.id.replace("_", "-") == pio_board.replace("_", "-")


def test_shared_pio_boards_resolve_to_a_canonical_entry(real_catalog: BoardCatalog) -> None:
    """Every PlatformIO board with >1 catalog entry resolves to a canonical one.

    The bug surfaced when a bare ``board: cb3s`` YAML matched only
    vendor products. Pin that any shared pio_board resolves to the
    generic/id-matching entry, so a future sync that imports a vendor
    device on a generic-less pio_board fails here instead of silently
    rendering its product card on unrelated configs.
    """
    groups: dict[tuple[str, str], list[BoardCatalogIndex]] = defaultdict(list)
    for board in real_catalog._boards:
        groups[(board.esphome.platform.value, board.esphome.board)].append(board)

    offenders = []
    for (platform, pio), entries in groups.items():
        if len(entries) < 2:
            continue
        winner = real_catalog.find_by_pio_board(pio, "", platform)
        if winner is None or not _is_canonical(winner, pio):
            offenders.append(
                (platform, pio, winner.id if winner else None, sorted(e.id for e in entries))
            )

    assert offenders == [], f"shared pio_boards resolving to a vendor product: {offenders!r}"


def test_cb3s_resolves_to_generic_module(real_catalog: BoardCatalog) -> None:
    """``board: cb3s`` resolves to the generic CB3S module, not a vendor product.

    Regression for the AVATTO-S06 board card rendering on a
    hand-written cb3s config; both share PlatformIO board ``cb3s``.
    """
    winner = real_catalog.find_by_pio_board("cb3s", "", "bk72xx")

    assert winner is not None
    assert winner.id == "cb3s"


def test_featured_boards_never_lose_to_a_non_canonical_entry(real_catalog: BoardCatalog) -> None:
    """A featured board wins its pio_board lookup, or defers only to a canonical entry.

    A featured vendor board built on a generic reference design
    (e.g. apollo-esk-1 on ``esp32-c6-devkitm-1``) intentionally
    defers to the generic for derivation; it's pick-only, since a
    bare ``board:`` config can't be told from a plain devkit. What
    must never happen is a featured board losing to *another* vendor
    product, which would render the wrong card.
    """
    offenders = []
    for board in real_catalog._boards:
        if not board.featured:
            continue
        variant = board.esphome.variant.value if board.esphome.variant else ""
        winner = real_catalog.find_by_pio_board(
            board.esphome.board, variant, board.esphome.platform.value
        )
        if winner is None:
            offenders.append((board.id, board.esphome.board, None))
        elif winner.id != board.id and not _is_canonical(winner, board.esphome.board):
            offenders.append((board.id, board.esphome.board, winner.id))

    assert offenders == [], f"featured boards losing to a non-canonical entry: {offenders!r}"
