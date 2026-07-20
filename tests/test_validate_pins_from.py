"""Cross-board validation of the ``pins_from`` manifest key."""

from __future__ import annotations

import script.validate_definitions as vd

_PIN = {"gpio": 4, "label": "GPIO4"}


def _board(pins_from: str | None = None, pins: list | None = None, variant: str = "esp32") -> dict:
    data: dict = {"esphome": {"platform": "esp32", "board": "esp32dev", "variant": variant}}
    if pins_from is not None:
        data["pins_from"] = pins_from
    if pins is not None:
        data["pins"] = pins
    return data


def test_valid_reference_is_quiet() -> None:
    donors = {"donor": _board(pins=[_PIN])}
    assert vd._validate_pins_from("b", _board(pins_from="donor"), [], donors) == []


def test_unknown_donor_id_fails() -> None:
    assert vd._validate_pins_from("b", _board(pins_from="ghost"), [], {}) == [
        "b: pins_from 'ghost' is not a known board"
    ]


def test_pinless_donor_fails() -> None:
    donors = {"donor": _board()}
    errors = vd._validate_pins_from("b", _board(pins_from="donor"), [], donors)
    assert errors == ["b: pins_from 'donor' has no pin table"]


def test_cross_chip_donor_fails() -> None:
    donors = {"donor": _board(pins=[_PIN], variant="esp32s3")}
    errors = vd._validate_pins_from("b", _board(pins_from="donor"), [], donors)
    assert errors == ["b: pins_from 'donor' is a different chip"]


def test_pins_from_with_own_pins_fails() -> None:
    donors = {"donor": _board(pins=[_PIN])}
    errors = vd._validate_pins_from("b", _board(pins_from="donor", pins=[_PIN]), [_PIN], donors)
    assert errors == ["b: pins_from and pins are mutually exclusive"]


def test_absent_key_is_quiet() -> None:
    assert vd._validate_pins_from("b", _board(), [], {}) == []
