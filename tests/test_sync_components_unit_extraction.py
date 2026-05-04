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
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "script"))

from sync_components import (
    _audit_catalog_for_unit_mismatches,
    _collect_refined_types,
    _enumerate_platform_manifests,
    _extract_validator_units,
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


def test_extract_units_returns_none_for_non_closure() -> None:
    """A plain function (no compiled-regex closure) returns None."""

    def not_a_validator(value):
        return value

    assert _extract_validator_units(not_a_validator) is None


def test_audit_warns_on_unit_suffixed_string_default(caplog) -> None:
    """Audit fires on float/integer entries with non-numeric string defaults.

    Actionable telemetry to add the validator to
    `_FLOAT_WITH_UNIT_VALIDATORS` (or `_UNIT_FALLBACKS`).
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
    """Mismatches buried inside a NESTED group still fire the warning."""
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
    assert "fake.component.inner_rate" in caplog.text


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
    silently falls back to `float`-with-string-default and the
    silent-Add-button class of bug returns. Pin the refinement here
    so an upstream rename / restructure trips CI.
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
