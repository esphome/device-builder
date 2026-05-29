"""Tests for the sync script's PinFeature filtering."""

from __future__ import annotations

import pytest

from script.sync_components import _resolve_pin_features  # type: ignore[import-not-found]


def test_resolve_pin_features_drops_gpio_mode_flags() -> None:
    """GPIO mode flags don't belong on ``ConfigEntry.pin_features``.

    The schema's ``modes`` list mixes hardware capabilities with
    configuration modes; only the former survive the filter so the
    mashumaro-strict ``ComponentCatalogEntry.from_dict`` roundtrip
    doesn't reject the catalog.
    """
    assert _resolve_pin_features({"modes": ["input", "output", "pullup"]}) == []


def test_resolve_pin_features_keeps_hardware_capabilities() -> None:
    assert _resolve_pin_features({"modes": ["adc", "dac", "pwm", "i2c_sda"]}) == [
        "adc",
        "dac",
        "pwm",
        "i2c_sda",
    ]


def test_resolve_pin_features_filters_mixed_input() -> None:
    """Real schema entries mix the two; the filter keeps only valid ones."""
    assert _resolve_pin_features({"modes": ["output", "uart_tx", "pullup", "pwm"]}) == [
        "uart_tx",
        "pwm",
    ]


@pytest.mark.parametrize("raw", [{}, {"modes": None}, {"modes": []}])
def test_resolve_pin_features_handles_missing_or_empty(raw: dict) -> None:
    assert _resolve_pin_features(raw) == []


def test_resolve_pin_features_drops_non_string_items() -> None:
    assert _resolve_pin_features({"modes": [None, 5, True, "adc"]}) == ["adc"]
