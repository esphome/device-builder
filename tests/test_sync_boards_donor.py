"""Donor pin inheritance for pin-less boards."""

from __future__ import annotations

import pytest

from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardCatalogResponse,
    BoardEsphomeConfig,
    BoardPin,
    Platform,
)
from script.sync_boards import _backfill_donor_pins

pytestmark = pytest.mark.xdist_group("board_sync")

_RECIPIENTS = {
    "esp32-generic-bluetooth-proxy": "generic-esp32",
    "esp32-generic-c3-bluetooth-proxy": "generic-esp32c3",
    "esp32-generic-c6-bluetooth-proxy": "generic-esp32c6",
    "esp32-generic-s3-bluetooth-proxy": "generic-esp32s3",
    "4moms_mamaroo_4": "generic-esp32c3",
    "levoit_core_300s": "generic-esp32",
}


@pytest.mark.parametrize(("recipient", "donor"), sorted(_RECIPIENTS.items()))
def test_pinless_board_inherits_the_generic_donor_table(
    generated_board_catalog: BoardCatalogResponse,
    recipient: str,
    donor: str,
) -> None:
    boards = {b.id: b for b in generated_board_catalog.boards}
    assert boards[recipient].pins, f"{recipient} should inherit pins"
    assert boards[recipient].pins == boards[donor].pins
    # A fresh list, so a later per-board pass can't mutate the donor's table.
    assert boards[recipient].pins is not boards[donor].pins


def test_atom_proxies_stay_empty(
    generated_board_catalog: BoardCatalogResponse,
) -> None:
    """
    The atom proxies stay empty by donor absence, not the ambiguity guard.

    No ``is_generic`` board carries their PIO board ids; a generic atom board
    added later would flip these to inheriting, which must be an explicit
    decision, not a silent one.
    """
    boards = {b.id: b for b in generated_board_catalog.boards}
    assert boards["m5stack-atom-lite-bluetooth-proxy"].pins == []
    assert boards["m5stack-atom-s3-bluetooth-proxy"].pins == []


def _entry(board_id: str, *, pins: int = 0, is_generic: bool = False) -> BoardCatalogEntry:
    return BoardCatalogEntry(
        id=board_id,
        name=board_id,
        description="",
        manufacturer="",
        esphome=BoardEsphomeConfig(platform=Platform.ESP32, board="esp32dev"),
        pins=[BoardPin(gpio=n, label=f"GPIO{n}") for n in range(pins)],
        is_generic=is_generic,
    )


def test_multiple_generic_donors_leave_the_board_empty() -> None:
    """The ambiguity guard itself: two same-chip generics, no inheritance."""
    recipient = _entry("recipient")
    boards = [
        _entry("donor-a", pins=3, is_generic=True),
        _entry("donor-b", pins=5, is_generic=True),
        recipient,
    ]
    _backfill_donor_pins(boards)
    assert recipient.pins == []


def test_curated_pins_are_never_overwritten(
    generated_board_catalog: BoardCatalogResponse,
) -> None:
    """A board with its own table keeps it even when a generic donor exists."""
    boards = {b.id: b for b in generated_board_catalog.boards}
    # m5stack-atom-echo shares generic-esp32's chip triple upstream of any
    # donor logic but curates its own restricted 6-pin table.
    echo = boards["m5stack-atom-echo"]
    assert echo.pins
    assert echo.pins != boards["generic-esp32"].pins
