"""Tests for the sync script's pin-field capability metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import esphome_device_builder
from esphome_device_builder.models.common import PinFeature, PinMode
from script.sync_components import (  # type: ignore[import-not-found]
    _PIN_FEATURE_OVERRIDES,
    _PIN_MODE_OVERRIDES,
    _apply_pin_constraints,
    _resolve_pin_features,
)

_COMPONENTS_DIR = Path(esphome_device_builder.__file__).parent / "definitions" / "components"


# --- _resolve_pin_features: schema ``modes`` -> hardware-capability filter ---


def test_resolve_pin_features_drops_gpio_mode_flags() -> None:
    """GPIO mode flags don't belong on ``ConfigEntry.pin_features``."""
    assert _resolve_pin_features({"modes": ["input", "output", "pullup"]}) == []


def test_resolve_pin_features_keeps_hardware_capabilities() -> None:
    assert _resolve_pin_features({"modes": ["adc", "dac", "pwm", "i2c_sda"]}) == [
        "adc",
        "dac",
        "pwm",
        "i2c_sda",
    ]


def test_resolve_pin_features_filters_mixed_input() -> None:
    assert _resolve_pin_features({"modes": ["output", "uart_tx", "pullup", "pwm"]}) == [
        "uart_tx",
        "pwm",
    ]


@pytest.mark.parametrize("raw", [{}, {"modes": None}, {"modes": []}])
def test_resolve_pin_features_handles_missing_or_empty(raw: dict) -> None:
    assert _resolve_pin_features(raw) == []


def test_resolve_pin_features_drops_non_string_items() -> None:
    assert _resolve_pin_features({"modes": [None, 5, True, "adc"]}) == ["adc"]


# --- override tables: one mapping per capability category is pinned so a
# regen that drops the table (or an accidental edit) fails CI ---


@pytest.mark.parametrize(
    ("component_id", "key", "expected"),
    [
        ("sensor.adc", "pin", [PinFeature.ADC]),
        ("output.esp32_dac", "pin", [PinFeature.DAC]),
        ("i2c", "sda", [PinFeature.I2C_SDA]),
        ("i2c", "scl", [PinFeature.I2C_SCL]),
        ("uart", "tx_pin", [PinFeature.UART_TX]),
        ("uart", "rx_pin", [PinFeature.UART_RX]),
        ("binary_sensor.esp32_touch", "pin", [PinFeature.TOUCH]),
        ("output.ledc", "pin", [PinFeature.PWM]),
    ],
)
def test_pin_feature_overrides_cover_each_category(
    component_id: str, key: str, expected: list[PinFeature]
) -> None:
    assert _PIN_FEATURE_OVERRIDES[(component_id, key)] == expected


@pytest.mark.parametrize(
    ("component_id", "key", "expected"),
    [
        ("output.gpio", "pin", PinMode.OUTPUT),
        ("switch.gpio", "pin", PinMode.OUTPUT),
        ("binary_sensor.gpio", "pin", PinMode.INPUT),
        ("stepper.a4988", "step_pin", PinMode.OUTPUT),
        ("stepper.uln2003", "pin_a", PinMode.OUTPUT),
    ],
)
def test_pin_mode_overrides(component_id: str, key: str, expected: PinMode) -> None:
    assert _PIN_MODE_OVERRIDES[(component_id, key)] == expected


# --- _apply_pin_constraints: emission-time application onto an entry dict ---


def test_apply_pin_constraints_sets_features() -> None:
    entry = {"type": "pin", "key": "pin", "pin_features": [], "pin_mode": None}
    _apply_pin_constraints(entry, "sensor.adc", "pin")
    assert entry["pin_features"] == ["adc"]
    assert entry["pin_mode"] is None


def test_apply_pin_constraints_sets_mode() -> None:
    entry = {"type": "pin", "key": "pin", "pin_features": [], "pin_mode": None}
    _apply_pin_constraints(entry, "binary_sensor.gpio", "pin")
    assert entry["pin_mode"] == "input"


def test_apply_pin_constraints_noop_for_unknown_field() -> None:
    entry = {"type": "pin", "key": "pin", "pin_features": [], "pin_mode": None}
    _apply_pin_constraints(entry, "sensor.unknown", "pin")
    assert entry["pin_features"] == []
    assert entry["pin_mode"] is None


def test_apply_pin_constraints_noop_for_non_pin_entry() -> None:
    entry = {"type": "string", "key": "pin", "pin_features": [], "pin_mode": None}
    _apply_pin_constraints(entry, "sensor.adc", "pin")
    assert entry["pin_features"] == []


# --- every curated mapping targets a real pin field in the shipped catalog,
# so a typo here or a schema rename that moves the field is caught in CI ---


def _pin_keys_in_component(component_id: str) -> set[str]:
    path = _COMPONENTS_DIR / f"{component_id}.json"
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        entry.get("key")
        for entry in (data.get("config_entries") or [])
        if isinstance(entry, dict) and entry.get("type") == "pin"
    }


@pytest.mark.parametrize(
    ("component_id", "key"),
    sorted(set(_PIN_FEATURE_OVERRIDES) | set(_PIN_MODE_OVERRIDES)),
)
def test_override_keys_exist_as_pin_entries_in_catalog(component_id: str, key: str) -> None:
    assert key in _pin_keys_in_component(component_id), (
        f"{component_id}.{key} is not a pin entry in the committed catalog"
    )
