"""Tests for the sync script's pin-field capability metadata."""

from __future__ import annotations

import pytest
from esphome import pins

from esphome_device_builder.models.common import PinFeature, PinMode
from script.sync_components import (  # type: ignore[import-not-found]
    _apply_pin_constraints,
    _collect_pin_constraints,
    _get_esphome_loader,
    _pin_constraint_from_validator,
    _PinConstraint,
    _resolve_pin_features,
)

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


@pytest.mark.parametrize("raw", [{}, {"modes": None}, {"modes": []}])
def test_resolve_pin_features_handles_missing_or_empty(raw: dict) -> None:
    assert _resolve_pin_features(raw) == []


# --- _pin_constraint_from_validator: direction off the gpio-schema closure ---


@pytest.mark.parametrize(
    ("validator", "expected_mode"),
    [
        (pins.gpio_input_pin_schema, PinMode.INPUT),
        (pins.gpio_output_pin_schema, PinMode.OUTPUT),
        (pins.gpio_pin_schema({"input": True, "output": True}), PinMode.INPUT_OUTPUT),
    ],
)
def test_pin_constraint_derives_direction(validator: object, expected_mode: PinMode) -> None:
    constraint = _pin_constraint_from_validator(validator)
    assert constraint.mode == expected_mode
    assert constraint.features == ()


def test_pin_constraint_non_pin_validator_is_empty() -> None:
    constraint = _pin_constraint_from_validator(str)
    assert constraint.mode is None
    assert constraint.features == ()


# --- _collect_pin_constraints: derivation from the component's live schema.
# These walk the installed esphome package, so they pin the contract against
# ESPHome's real pin validators rather than a hand-maintained table. ---


def test_collect_derives_input_and_output_without_cross_contamination() -> None:
    """``gpio`` ships as input (binary_sensor) and output (output); they must not collide."""
    loader = _get_esphome_loader()
    bsensor = _collect_pin_constraints(loader, "binary_sensor", "gpio", "gpio.binary_sensor")
    output = _collect_pin_constraints(loader, "output", "gpio", "gpio.output")
    assert bsensor[("pin",)].mode == PinMode.INPUT
    assert output[("pin",)].mode == PinMode.OUTPUT


@pytest.mark.parametrize(
    ("domain", "stem", "top_key", "feature"),
    [
        ("sensor", "adc", "adc.sensor", PinFeature.ADC),
        ("output", "esp32_dac", "esp32_dac.output", PinFeature.DAC),
        ("binary_sensor", "esp32_touch", "esp32_touch.binary_sensor", PinFeature.TOUCH),
    ],
)
def test_collect_derives_fixed_silicon_features(
    domain: str, stem: str, top_key: str, feature: PinFeature
) -> None:
    constraints = _collect_pin_constraints(_get_esphome_loader(), domain, stem, top_key)
    assert feature in constraints[("pin",)].features


def test_collect_omits_matrix_routed_bus_capabilities() -> None:
    """i2c sda/scl route through the GPIO matrix — emitting a feature would wrongly filter."""
    constraints = _collect_pin_constraints(_get_esphome_loader(), None, "i2c", "i2c")
    for constraint in constraints.values():
        assert PinFeature.I2C_SDA not in constraint.features
        assert PinFeature.I2C_SCL not in constraint.features


def test_collect_returns_empty_without_loader() -> None:
    assert _collect_pin_constraints(None, "output", "gpio", "gpio.output") == {}


# --- _apply_pin_constraints: stamping derived constraints onto catalog entries ---


def _entry(key: str = "pin", **extra: object) -> dict:
    return {"type": "pin", "key": key, **extra}


def test_apply_stamps_mode_and_features() -> None:
    entries = [_entry()]
    loader = _get_esphome_loader()
    constraints = _collect_pin_constraints(loader, "output", "esp32_dac", "esp32_dac.output")
    _apply_pin_constraints(entries, constraints)
    assert entries[0]["pin_mode"] == "output"
    assert entries[0]["pin_features"] == ["dac"]


def test_apply_only_targets_pin_typed_entries() -> None:
    entries = [{"type": "string", "key": "pin"}]
    _apply_pin_constraints(entries, {("pin",): _PinConstraint(PinMode.OUTPUT, ())})
    assert "pin_mode" not in entries[0]


def test_apply_merges_without_duplicating_existing_features() -> None:
    entries = [_entry(pin_features=["adc"])]
    _apply_pin_constraints(
        entries, {("pin",): _PinConstraint(None, (PinFeature.ADC, PinFeature.DAC))}
    )
    assert entries[0]["pin_features"] == ["adc", "dac"]


def test_apply_is_noop_for_unconstrained_path() -> None:
    entries = [_entry()]
    _apply_pin_constraints(entries, {})
    assert "pin_mode" not in entries[0]
