"""Donor pin inheritance for pin-less boards (#2219)."""

from __future__ import annotations

import pytest

from esphome_device_builder.models import BoardCatalogResponse

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


def test_ambiguous_donors_stay_empty(
    generated_board_catalog: BoardCatalogResponse,
) -> None:
    """The atom module's products expose 6-34 pins; no single right donor."""
    boards = {b.id: b for b in generated_board_catalog.boards}
    assert boards["m5stack-atom-lite-bluetooth-proxy"].pins == []
    assert boards["m5stack-atom-s3-bluetooth-proxy"].pins == []


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
