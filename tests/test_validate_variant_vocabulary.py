"""Pins the manifest validator's snapshot-backed variant vocabulary gate."""

from __future__ import annotations

from script.validate_definitions import (  # type: ignore[import-not-found]
    _ESP32_VARIANTS,
    _validate_variant_vocabulary,
)


def test_snapshot_vocabulary_loaded() -> None:
    assert "esp32c3" in _ESP32_VARIANTS
    assert "esp32s31" in _ESP32_VARIANTS  # missing from the old hand schema enum


def test_known_variant_passes_any_spelling() -> None:
    assert _validate_variant_vocabulary("b", {"esphome": {"variant": "esp32c3"}}) == []
    assert _validate_variant_vocabulary("b", {"esphome": {"variant": "ESP32-C3"}}) == []


def test_unknown_variant_rejected() -> None:
    errors = _validate_variant_vocabulary("b", {"esphome": {"variant": "esp32z9"}})
    assert errors and "not a known esp32 variant" in errors[0]


def test_absent_variant_passes() -> None:
    assert _validate_variant_vocabulary("b", {"esphome": {}}) == []
    assert _validate_variant_vocabulary("b", {}) == []
    # Malformed shapes are the schema step's job; the manual check stays quiet.
    assert _validate_variant_vocabulary("b", {"esphome": ["not-a-dict"]}) == []
