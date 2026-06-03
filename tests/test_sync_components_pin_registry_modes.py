"""
Tests for the per-provider pin ``mode`` flag derivation.

The long-form pin Mode checkboxes a value supports depend on its pin
provider: an I2C expander like ``pca9554`` allows only ``input`` /
``output``, while a native pin allows all five flags. The sync
introspects ESPHome's live ``PIN_SCHEMA_REGISTRY`` to emit a global
``{provider_key: [allowed_modes]}`` map (excluding native target
platforms) the frontend scopes against.

These walk the installed esphome package, so they pin the contract
against ESPHome's real pin schemas rather than a hand-maintained table.
"""

from __future__ import annotations

import voluptuous as vol
from esphome.const import Platform

from script.sync_components import (  # type: ignore[import-not-found]
    _build_pin_registry_modes,
    _pin_registry_allowed_modes,
    _pin_schema_mode_mapping,
)


def test_pin_schema_mode_mapping_unwraps_schema_and_all() -> None:
    mapping = {vol.Optional("input"): bool}
    assert _pin_schema_mode_mapping(mapping) is mapping
    assert _pin_schema_mode_mapping(vol.Schema(mapping)) == mapping
    assert _pin_schema_mode_mapping(vol.All(vol.Schema(mapping), str)) == mapping


def test_pin_schema_mode_mapping_returns_none_for_scalar() -> None:
    assert _pin_schema_mode_mapping("nope") is None
    assert _pin_schema_mode_mapping(123) is None


def test_pin_registry_allowed_modes_reads_mode_flag_keys() -> None:
    schema = vol.Schema(
        {
            vol.Required("number"): int,
            vol.Optional("mode"): vol.All(
                {vol.Optional("input"): bool, vol.Optional("output"): bool},
                lambda v: v,
            ),
        }
    )
    assert _pin_registry_allowed_modes(schema) == ["input", "output"]


def test_pin_registry_allowed_modes_none_without_mode_key() -> None:
    assert _pin_registry_allowed_modes(vol.Schema({vol.Required("number"): int})) is None


def test_build_derives_external_provider_modes() -> None:
    modes = _build_pin_registry_modes(
        ["esp32", "pca9554", "pcf8574", "mcp23017", "sx1509", "sn74hc595", "sn74hc165"]
    )
    # Expanders are direction-only.
    assert modes["pca9554"] == ["input", "output"]
    assert modes["pcf8574"] == ["input", "output"]
    # mcp23017 registers under the shared ``mcp23xxx`` key and adds pullup.
    assert "pullup" in modes["mcp23xxx"]
    assert "pulldown" not in modes["mcp23xxx"]
    # Shift registers are single-direction.
    assert modes["sn74hc595"] == ["output"]
    assert modes["sn74hc165"] == ["input"]


def test_build_excludes_native_target_platforms() -> None:
    """Native platforms allow every flag, so only external providers are emitted.

    Scoping a native pin would be a no-op; the filter drops them whether the
    platform registers under the ``Platform`` enum (esp32) or a bare string
    (rp2040 / bk72xx).
    """
    modes = _build_pin_registry_modes(["esp32", "esp8266", "rp2040", "host", "pca9554"])
    platform_names = {p.value for p in Platform}
    assert not platform_names & modes.keys()
    assert "pca9554" in modes


def test_build_returns_sorted_flag_lists() -> None:
    modes = _build_pin_registry_modes(["sx1509"])
    assert modes["sx1509"] == sorted(modes["sx1509"])
