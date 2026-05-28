"""Sub-readings on multi-sensor platforms surface on the main form."""

from __future__ import annotations

import json
from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    _convert_field,
)

_UNUSED_SCHEMA_DIR = Path("/unused")
_BODIES_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)


def _load_body(component_id: str) -> dict:
    """Read one component's split body file off disk."""
    return json.loads((_BODIES_DIR / f"{component_id}.json").read_text(encoding="utf-8"))


def _convert(raw: dict) -> dict:
    entry = _convert_field("temperature", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    return entry


def test_sub_reading_extends_overrides_advanced_to_false() -> None:
    """A field whose schema extends a sensor base lands not-advanced (#983)."""
    raw = {
        "key": "Optional",
        "type": "schema",
        "schema": {
            "config_vars": {"name": {"key": "Required"}},
            "extends": ["sensor._SENSOR_SCHEMA"],
        },
    }
    assert _convert(raw)["advanced"] is False


def test_binary_sensor_and_text_sensor_bases_also_override() -> None:
    """The override applies to all three multi-sensor base schemas."""
    for base in (
        "binary_sensor._BINARY_SENSOR_SCHEMA",
        "text_sensor._TEXT_SENSOR_SCHEMA",
    ):
        raw = {
            "key": "Optional",
            "type": "schema",
            "schema": {"config_vars": {}, "extends": [base]},
        }
        assert _convert(raw)["advanced"] is False, f"failed for {base}"


def test_non_sub_reading_nested_keeps_default_advanced() -> None:
    """A nested field that does NOT extend a sensor base stays as classified."""
    raw = {
        "key": "Optional",
        "type": "schema",
        "schema": {
            "config_vars": {"scan_window": {"key": "Optional"}},
            # Different base — e.g. a plain config block.
            "extends": ["esp32_ble_tracker._SCAN_PARAMETERS_SCHEMA"],
        },
    }
    # ``_classify_advanced`` defaults optional fields to advanced —
    # the override doesn't touch this case.
    assert _convert(raw)["advanced"] is True


def test_no_extends_field_unaffected() -> None:
    """Fields with no ``extends`` reference are untouched by the override."""
    raw = {
        "key": "Optional",
        "type": "string",
    }
    # No extends → no override → falls back to ``_classify_advanced``.
    # ``temperature`` isn't in IMPORTANT_KEYS or ADVANCED_BASE_KEYS, so
    # ``_classify_advanced`` returns the default ``True`` for optionals.
    assert _convert(raw)["advanced"] is True


def test_catalog_dht_sub_readings_not_advanced() -> None:
    """Real catalog: DHT temperature + humidity surface on the main form."""
    dht = _load_body("sensor.dht")
    by_key = {e["key"]: e for e in dht["config_entries"]}
    # ``advanced: False`` is the default and gets stripped by
    # ``_strip_entry_defaults``; treat absent as False.
    assert by_key["temperature"].get("advanced", False) is False
    assert by_key["humidity"].get("advanced", False) is False


def test_catalog_debug_sub_readings_not_advanced_but_id_stays() -> None:
    """All 7 debug sub-readings surface; ``debug_id`` (an ID) stays advanced."""
    debug = _load_body("sensor.debug")
    by_key = {e["key"]: e for e in debug["config_entries"]}
    sub_readings = (
        "block",
        "cpu_frequency",
        "fragmentation",
        "free",
        "loop_time",
        "min_free",
        "psram",
    )
    for key in sub_readings:
        assert by_key[key].get("advanced", False) is False, f"{key} should not be advanced"
    # ``debug_id`` is the platform's GeneratedID field, not a
    # sub-reading; the override doesn't reach it and the default
    # classification still applies. ``True`` is non-default so the
    # key is preserved on the catalog dict.
    assert by_key["debug_id"]["advanced"] is True
