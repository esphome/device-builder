"""Pins the manifest validator's snapshot-backed variant vocabulary gate."""

from __future__ import annotations

from pathlib import Path

from script.validate_definitions import (  # type: ignore[import-not-found]
    _ESP32_VARIANTS,
    _validate_variant_vocabulary,
    validate_board,
)


def test_snapshot_vocabulary_loaded() -> None:
    assert _ESP32_VARIANTS, "committed snapshot should carry the vocabulary"
    assert all(v.startswith("esp32") for v in _ESP32_VARIANTS)
    assert "esp32" in _ESP32_VARIANTS  # the classic chip is always a member


def test_canonical_variant_passes() -> None:
    assert _validate_variant_vocabulary("b", {"esphome": {"variant": "esp32c3"}}) == []


def test_non_canonical_spelling_gets_the_friendly_error() -> None:
    for spelling in ("ESP32-C3", "esp32_c3", "ESP32C3"):
        errors = _validate_variant_vocabulary("b", {"esphome": {"variant": spelling}})
        assert errors and "canonical lowercase spelling 'esp32c3'" in errors[0]


def test_unknown_variant_rejected() -> None:
    errors = _validate_variant_vocabulary("b", {"esphome": {"variant": "esp32z9"}})
    assert errors and "not a known esp32 variant" in errors[0]


def test_absent_variant_passes() -> None:
    assert _validate_variant_vocabulary("b", {"esphome": {}}) == []
    assert _validate_variant_vocabulary("b", {}) == []
    # Malformed shapes are the schema step's job; the manual check stays quiet.
    assert _validate_variant_vocabulary("b", {"esphome": ["not-a-dict"]}) == []


def test_gate_is_wired_into_validate_board() -> None:
    """The unknown-variant diagnostic surfaces through the public walk, not just the helper."""
    data = {
        "id": "fake-board",
        "name": "Fake",
        "description": "d",
        "esphome": {"platform": "esp32", "board": "esp32dev", "variant": "esp32z9"},
    }
    errors = validate_board(Path("fake-board/manifest.yaml"), data=data, all_boards={})
    assert any("not a known esp32 variant" in e for e in errors)
