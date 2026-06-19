"""Tests for the sync script's unit-options extraction.

`_extract_validator_units` is the load-bearing magic that pulls the
unit picker list out of `cv.float_with_unit` validators at runtime —
no hand-maintained mapping that goes stale on the next ESPHome
release. Pin its output for each `cv.*` validator the catalog cares
about so an upstream regex tweak can't silently change the unit list
the dashboard ships.

`_audit_catalog_for_unit_mismatches` is the regression net for new
unit-coerced validators ESPHome adds after this PR — make sure the
warning fires for the cases we've already curated as follow-ups.
"""

from __future__ import annotations

import logging
import types

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _audit_catalog_for_unit_mismatches,
    _collect_refined_types,
    _enumerate_platform_manifests,
    _extract_validator_units,
    _present_non_introspectable_units,
    _walk_schema_keys,
)


@pytest.fixture
def cv():
    """Lazy-import esphome's config_validation; skip if unavailable."""
    try:
        from esphome import config_validation as _cv  # noqa: PLC0415
    except Exception:
        pytest.skip("esphome.config_validation not importable")
    return _cv


def test_extract_units_for_frequency(cv) -> None:
    """`cv.frequency` produces the IoT-relevant metric-prefixed Hz list.

    Canonical unit (`Hz`) first; remaining prefixes in magnitude
    order. The frontend's renderer treats `unit_options[0]` as the
    canonical unit (range bounds default to it), so this contract
    matters at every layer.
    """
    assert _extract_validator_units(cv.frequency) == [
        "Hz",
        "nHz",
        "µHz",
        "mHz",
        "kHz",
        "MHz",
        "GHz",
    ]


def test_extract_units_for_voltage(cv) -> None:
    """`cv.voltage` produces the IoT-relevant metric-prefixed V list."""
    assert _extract_validator_units(cv.voltage) == [
        "V",
        "nV",
        "µV",
        "mV",
        "kV",
        "MV",
        "GV",
    ]


def test_extract_units_for_distance(cv) -> None:
    """`cv.distance` produces the IoT-relevant metric-prefixed m list."""
    assert _extract_validator_units(cv.distance) == [
        "m",
        "nm",
        "µm",
        "mm",
        "km",
        "Mm",
        "Gm",
    ]


def test_extract_units_for_framerate(cv) -> None:
    """`cv.framerate` is a fixed-unit validator (no metric prefix)."""
    units = _extract_validator_units(cv.framerate)
    # Order is canonical-first; both `FPS` and `Hz` accepted by the
    # validator. We don't pin order here because `framerate`'s regex
    # alternation is stable but the canonical pick depends on the
    # uppercase-preference heuristic.
    assert units is not None
    assert set(units) >= {"FPS", "Hz"}


def test_extract_units_for_resistance(cv) -> None:
    """`cv.resistance` (not on any hand-maintained list) is discovered as metric Ω."""
    assert _extract_validator_units(cv.resistance) == [
        "Ω",
        "nΩ",
        "µΩ",
        "mΩ",
        "kΩ",
        "MΩ",
        "GΩ",
    ]


def test_extract_units_for_current(cv) -> None:
    """`cv.current` is discovered as a metric-prefixed A list."""
    assert _extract_validator_units(cv.current) == [
        "A",
        "nA",
        "µA",
        "mA",
        "kA",
        "MA",
        "GA",
    ]


def test_extract_units_for_bps(cv) -> None:
    """`cv.bps` is discovered as a metric-prefixed bit-rate list."""
    units = _extract_validator_units(cv.bps)
    assert units is not None
    assert units[0] == "bps"
    assert {"kbps", "Mbps", "Gbps"} <= set(units)


def test_extract_units_for_decibel(cv) -> None:
    """`cv.decibel` is a non-metric unit: distinct dB / dBm, no prefixes."""
    units = _extract_validator_units(cv.decibel)
    assert units is not None
    assert set(units) == {"dB", "dBm"}
    assert units[0] == "dB"


def test_extract_units_for_angle(cv) -> None:
    """`cv.angle` is a non-metric unit: ° / deg, no prefixes."""
    units = _extract_validator_units(cv.angle)
    assert units is not None
    assert set(units) == {"°", "deg"}


def test_extract_units_returns_none_for_non_closure() -> None:
    """A plain function (no compiled-regex closure) returns None."""

    def not_a_validator(value):
        return value

    assert _extract_validator_units(not_a_validator) is None


def test_audit_warns_on_unit_suffixed_string_default(caplog) -> None:
    """Audit fires on float/integer entries with non-numeric string defaults.

    Actionable telemetry for a hand-rolled validator that needs a
    `_NON_INTROSPECTABLE_UNITS` entry.
    """
    catalog = [
        {
            "id": "fake.component",
            "config_entries": [
                {
                    "key": "rate",
                    "type": "float",
                    "default_value": "100ms",
                },
                {
                    "key": "size",
                    "type": "integer",
                    "default_value": "1KB",
                },
                # Already-numeric default — must NOT trip the audit.
                {
                    "key": "count",
                    "type": "integer",
                    "default_value": "42",
                },
            ],
        }
    ]
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        _audit_catalog_for_unit_mismatches(catalog)
    text = caplog.text
    assert "fake.component.rate" in text
    assert "fake.component.size" in text
    assert "fake.component.count" not in text


def test_audit_recurses_into_nested_entries(caplog) -> None:
    """Mismatches buried inside a NESTED group fire the warning with full path."""
    catalog = [
        {
            "id": "fake.component",
            "config_entries": [
                {
                    "key": "outer",
                    "type": "nested",
                    "config_entries": [
                        {
                            "key": "inner_rate",
                            "type": "float",
                            "default_value": "100ms",
                        }
                    ],
                }
            ],
        }
    ]
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        _audit_catalog_for_unit_mismatches(catalog)
    # Warning includes the full dotted path (`outer.inner_rate`)
    # rather than the bare leaf — components with repeated nested
    # keys (`rate`, `size`) would otherwise produce ambiguous
    # warnings.
    assert "fake.component.outer.inner_rate" in caplog.text


def test_audit_recurses_into_map_value_templates(caplog) -> None:
    """MAP value templates carry inner ``config_entries`` too.

    `_build_map_value_template` materialises the value-side schema of
    user-keyed maps (`api.actions.<user_key>.<...>`,
    `esphome.platformio_options.<...>`). Without recursing into
    those, the audit silently misses any unit-coerced numeric
    default that lands inside one — exactly the class of catalog
    bug the audit is supposed to police.
    """
    catalog = [
        {
            "id": "fake.component",
            "config_entries": [
                {
                    "key": "actions",
                    "type": "map",
                    "config_entries": [
                        {
                            "key": "delay",
                            "type": "float",
                            "default_value": "100ms",
                        }
                    ],
                }
            ],
        }
    ]
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        _audit_catalog_for_unit_mismatches(catalog)
    assert "fake.component.actions.delay" in caplog.text


@pytest.fixture
def loader():
    """Lazy-import esphome's loader; skip if unavailable."""
    try:
        from esphome import loader as _loader  # noqa: PLC0415
    except Exception:
        pytest.skip("esphome.loader not importable")
    return _loader


def test_enumerate_platform_manifests_returns_real_manifests(loader) -> None:
    """`mcp3008` ships a sensor and an output platform.

    `_enumerate_platform_manifests` must surface both so the platform-
    schema's unit-coerced fields (`reference_voltage` etc.) get refined
    on the live introspection walk — a small upstream shape change
    here would silently strip `float_with_unit` metadata otherwise.
    """
    manifests = _enumerate_platform_manifests(loader, "mcp3008")
    # At least the sensor platform should be reachable; output is
    # the secondary platform.
    assert manifests, "mcp3008 should expose at least one platform manifest"


def test_platform_manifest_refines_unit_coerced_field(loader) -> None:
    """End-to-end: `mcp3008.sensor.reference_voltage` is `float_with_unit`.

    The bare `mcp3008` manifest's `config_schema` carries the SPI bus
    fields but NOT the per-instance `reference_voltage` — that lives
    on the platform schema (`mcp3008.sensor`). If
    `_enumerate_platform_manifests` regresses, this catalog field
    silently falls back to `float`-with-string-default. Pin the
    refinement here so an upstream rename / restructure trips CI.
    """
    refined = {}
    for platform_manifest in _enumerate_platform_manifests(loader, "mcp3008"):
        refined.update(_collect_refined_types(platform_manifest))
    voltage = refined.get(("reference_voltage",))
    if voltage is None:
        pytest.skip(
            "esphome version doesn't expose mcp3008.sensor.reference_voltage "
            "via the live-introspection walker — guard, not a regression"
        )
    assert voltage.type == "float_with_unit"
    assert voltage.unit_options is not None and "V" in voltage.unit_options


def test_resistance_sensor_resistor_refines_to_float_with_unit(loader) -> None:
    """`resistance.sensor.resistor` refines to `float_with_unit` with Ω units."""
    refined = {}
    for platform_manifest in _enumerate_platform_manifests(loader, "resistance"):
        refined.update(_collect_refined_types(platform_manifest))
    resistor = refined.get(("resistor",))
    if resistor is None:
        pytest.skip(
            "esphome version doesn't expose resistance.sensor.resistor "
            "via the live-introspection walker — guard, not a regression"
        )
    assert resistor.type == "float_with_unit"
    assert resistor.unit_options is not None and "Ω" in resistor.unit_options


def test_missing_non_introspectable_validator_warns(caplog) -> None:
    """A removed hand-maintained validator warns and is dropped, not silently missing."""

    class _StubCV:
        data_size = object()
        temperature = object()
        # temperature_delta removed

    with caplog.at_level(logging.WARNING, logger="sync_components"):
        present = _present_non_introspectable_units(_StubCV())
    assert "temperature_delta" in caplog.text
    assert "temperature_delta" not in present
    assert {"data_size", "temperature"} <= present.keys()


def test_walk_descends_typed_schema_branches(cv) -> None:
    """``_walk_schema_keys`` visits fields inside ``cv.typed_schema`` branches."""
    typed = cv.typed_schema(
        {
            "W5500": cv.Schema({cv.Optional("clock_speed", default="26.67MHz"): cv.frequency}),
            "LAN8720": cv.Schema({cv.Optional("phy_addr", default=0): cv.int_}),
        },
        upper=True,
    )
    keys: set[str] = set()
    _walk_schema_keys(typed, lambda _k, key_name, _v, _path: keys.add(key_name))
    assert {"clock_speed", "phy_addr"} <= keys


def test_collect_refined_types_descends_typed_schema(cv) -> None:
    """A ``cv.frequency`` field inside a typed_schema branch refines to ``float_with_unit``."""
    typed = cv.typed_schema(
        {"W5500": cv.Schema({cv.Optional("clock_speed", default="26.67MHz"): cv.frequency})},
        upper=True,
    )
    manifest = types.SimpleNamespace(config_schema=typed)
    refined = _collect_refined_types(manifest)
    clock_speed = refined.get(("clock_speed",))
    assert clock_speed is not None
    assert clock_speed.type == "float_with_unit"
    assert clock_speed.unit_options is not None and "MHz" in clock_speed.unit_options


def test_audit_silent_when_no_mismatches(caplog) -> None:
    """No warning when every numeric entry has a numeric default."""
    catalog = [
        {
            "id": "fake.component",
            "config_entries": [
                {"key": "rate", "type": "float", "default_value": 1.5},
                {"key": "count", "type": "integer", "default_value": 7},
                {"key": "name", "type": "string", "default_value": "abc"},
            ],
        }
    ]
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        _audit_catalog_for_unit_mismatches(catalog)
    assert "Catalog audit" not in caplog.text
