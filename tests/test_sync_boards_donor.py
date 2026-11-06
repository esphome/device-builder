"""Donor pin inheritance for pin-less boards."""

from __future__ import annotations

import logging

import pytest

from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardCatalogResponse,
    BoardEsphomeConfig,
    BoardPin,
    Platform,
)
from script import sync_boards
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


@pytest.mark.parametrize(
    ("recipient", "donor"),
    [
        ("m5stack-atom-lite-bluetooth-proxy", "m5stack-atom-lite"),
        ("m5stack-atom-s3-bluetooth-proxy", "m5stack-atoms3"),
    ],
)
def test_pins_from_wires_the_atom_proxies(
    generated_board_catalog: BoardCatalogResponse,
    recipient: str,
    donor: str,
) -> None:
    """``pins_from`` names the product board the automatic rule can't pick."""
    boards = {b.id: b for b in generated_board_catalog.boards}
    assert boards[recipient].pins == boards[donor].pins
    assert boards[recipient].pins is not boards[donor].pins
    # The same-chip echo's restricted 6-pin table must not leak in.
    assert boards[recipient].pins != boards["m5stack-atom-echo"].pins


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
    _backfill_donor_pins(boards, pins_from={})
    assert recipient.pins == []


def test_explicit_pins_from_beats_the_automatic_rule() -> None:
    recipient = _entry("recipient")
    generic = _entry("generic", pins=3, is_generic=True)
    product = _entry("product", pins=5)
    _backfill_donor_pins([generic, product, recipient], pins_from={"recipient": "product"})
    assert recipient.pins == product.pins
    assert recipient.pins is not product.pins


def test_pins_from_with_a_missing_or_empty_donor_leaves_the_board_empty() -> None:
    """Sync stays lenient; validate_definitions is the loud gate."""
    recipient = _entry("recipient")
    hollow = _entry("hollow")
    _backfill_donor_pins([recipient, hollow], pins_from={"recipient": "ghost"})
    assert recipient.pins == []
    _backfill_donor_pins([recipient, hollow], pins_from={"recipient": "hollow"})
    assert recipient.pins == []


def test_manifest_scan_warns_on_an_unreadable_manifest(tmp_path, monkeypatch, caplog) -> None:
    good = tmp_path / "boards" / "good"
    good.mkdir(parents=True)
    (good / "manifest.yaml").write_text("id: good\npins_from: donor\n", encoding="utf-8")
    bad = tmp_path / "boards" / "bad"
    bad.mkdir()
    (bad / "manifest.yaml").write_text("{ not valid yaml\n", encoding="utf-8")
    monkeypatch.setattr(sync_boards, "_DEFINITIONS_DIR", tmp_path)
    with caplog.at_level(logging.WARNING, logger="sync_boards"):
        out = sync_boards._manifest_pins_from()
    assert out == {"good": "donor"}
    assert any("bad" in r.message and "Skipping" in r.message for r in caplog.records)


def test_pins_from_never_overwrites_curated_pins() -> None:
    keeper = _entry("keeper", pins=2)
    donor = _entry("donor", pins=5)
    kept = list(keeper.pins)
    _backfill_donor_pins([keeper, donor], pins_from={"keeper": "donor"})
    assert keeper.pins == kept


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
