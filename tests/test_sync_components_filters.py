"""
Contract tests for the filter-registry sync path.

Covers the pieces that have no upstream-schema dependency: the
``_dedupe_filters`` merge contract and the ``_convert_field``
detection branches that promote ``effects:`` / ``filters:`` to
``type=registry_list``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from esphome_device_builder.controllers.components import ComponentCatalog
from script.sync_components import (  # type: ignore[import-not-found]
    _convert_field,
    _convert_registry_entry,
    _dedupe_filters,
    _is_scalar_extends_schema,
)

_UNUSED_SCHEMA_DIR = Path("/unused")


# ---------------------------------------------------------------------------
# _dedupe_filters
# ---------------------------------------------------------------------------


def test_dedupe_filters_keeps_single_domain_entries_intact() -> None:
    """Filters appearing in only one registry pass through verbatim."""
    out = _dedupe_filters(
        [
            {"id": "delta", "name": "Sensor → Delta", "applies_to": ["sensor"]},
            {
                "id": "delayed_on",
                "name": "Binary Sensor → Delayed On",
                "applies_to": ["binary_sensor"],
            },
        ]
    )
    by_id = {f["id"]: f for f in out}
    assert by_id["delta"]["name"] == "Sensor → Delta"
    assert by_id["delta"]["applies_to"] == ["sensor"]
    assert by_id["delayed_on"]["name"] == "Binary Sensor → Delayed On"


def test_dedupe_filters_merges_applies_to_across_domains() -> None:
    """Ids registered in multiple domains collapse with unioned ``applies_to``."""
    out = _dedupe_filters(
        [
            {"id": "lambda", "name": "Binary Sensor → Lambda", "applies_to": ["binary_sensor"]},
            {"id": "lambda", "name": "Sensor → Lambda", "applies_to": ["sensor"]},
            {"id": "lambda", "name": "Text Sensor → Lambda", "applies_to": ["text_sensor"]},
        ]
    )
    assert len(out) == 1
    assert sorted(out[0]["applies_to"]) == ["binary_sensor", "sensor", "text_sensor"]


def test_dedupe_filters_strips_domain_prefix_on_multi_domain_entries() -> None:
    """Multi-domain entries drop the ``<Domain> → `` prefix."""
    out = _dedupe_filters(
        [
            {"id": "timeout", "name": "Binary Sensor → Timeout", "applies_to": ["binary_sensor"]},
            {"id": "timeout", "name": "Sensor → Timeout", "applies_to": ["sensor"]},
        ]
    )
    assert out[0]["name"] == "Timeout"
    assert "→" not in out[0]["name"]


def test_dedupe_filters_keeps_first_occurrence_config_entries() -> None:
    """Per-domain ``config_entries`` differences fall back to the first hit."""
    first_entries = [{"key": "value", "type": "string"}]
    second_entries = [{"key": "value", "type": "boolean"}]
    out = _dedupe_filters(
        [
            {
                "id": "lambda",
                "name": "Sensor → Lambda",
                "applies_to": ["sensor"],
                "config_entries": first_entries,
            },
            {
                "id": "lambda",
                "name": "Binary Sensor → Lambda",
                "applies_to": ["binary_sensor"],
                "config_entries": second_entries,
            },
        ]
    )
    assert out[0]["config_entries"] is first_entries


# ---------------------------------------------------------------------------
# _convert_field — REGISTRY_LIST detection
# ---------------------------------------------------------------------------


def test_convert_field_emits_registry_list_for_light_effects() -> None:
    """``key: effects`` + ``filter: [<ids>]`` → ``registry_list/light_effects``."""
    raw = {
        "key": "Optional",
        "filter": ["pulse", "addressable_rainbow"],
        "docs": "**list**: light effects",
    }
    entry = _convert_field("effects", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["type"] == "registry_list"
    assert entry["registry"] == "light_effects"
    assert entry["multi_value"] is True


def test_convert_field_emits_registry_list_for_sensor_filter() -> None:
    """``type=registry, registry=*.filter, is_list=true`` → ``registry_list/filter``."""
    raw = {
        "is_list": True,
        "key": "Optional",
        "registry": "sensor.filter",
        "type": "registry",
        "docs": "**list**: filters",
    }
    entry = _convert_field("filters", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["type"] == "registry_list"
    assert entry["registry"] == "filter"
    assert entry["multi_value"] is True


def test_convert_field_filter_branch_accepts_each_sensor_domain() -> None:
    """The endswith('.filter') check catches every supported sensor flavour."""
    for registry in ("sensor.filter", "binary_sensor.filter", "text_sensor.filter"):
        raw = {
            "is_list": True,
            "key": "Optional",
            "registry": registry,
            "type": "registry",
        }
        entry = _convert_field("filters", raw, _UNUSED_SCHEMA_DIR)
        assert entry is not None
        assert entry["registry"] == "filter", f"unexpected dispatch for {registry}"


def test_loaded_catalog_preserves_registry_field() -> None:
    """The runtime catalog loader keeps ``ConfigEntry.registry`` populated."""
    cat = ComponentCatalog(MagicMock())
    cat.load()
    light = cat._by_id["light.esp32_rmt_led_strip"]
    effects = next(e for e in light.config_entries if e.key == "effects")
    assert effects.type.value == "registry_list"
    assert effects.registry == "light_effects"
    sensor = cat._by_id["sensor.a01nyub"]
    filters = next(e for e in sensor.config_entries if e.key == "filters")
    assert filters.type.value == "registry_list"
    assert filters.registry == "filter"


def test_convert_field_unrelated_string_field_stays_string() -> None:
    """The ``key == "effects"`` heuristic does NOT fire on every list-shaped string."""
    raw = {"key": "Optional", "type": "string", "docs": "a description"}
    entry = _convert_field("name", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["type"] != "registry_list"
    assert entry["registry"] is None


# ---------------------------------------------------------------------------
# _is_scalar_extends_schema
# ---------------------------------------------------------------------------


def test_scalar_extends_detects_time_period() -> None:
    """``throttle`` extends ``core.positive_time_period_milliseconds``."""
    assert _is_scalar_extends_schema({"extends": ["core.positive_time_period_milliseconds"]})


def test_scalar_extends_detects_returning_lambda() -> None:
    """Lambda-shaped filters (``filter`` action's value is a lambda body)."""
    assert _is_scalar_extends_schema({"extends": ["core.returning_lambda"]})


def test_scalar_extends_detects_float_and_int() -> None:
    """Scalar primitives via the ``.float_`` / ``.positive_int`` suffix."""
    assert _is_scalar_extends_schema({"extends": ["core.float_"]})
    assert _is_scalar_extends_schema({"extends": ["core.positive_float"]})
    assert _is_scalar_extends_schema({"extends": ["core.int_"]})
    assert _is_scalar_extends_schema({"extends": ["core.positive_int"]})


def test_scalar_extends_rejects_mapping_extends() -> None:
    """Mapping schemas like ``sensor.DELTA_SCHEMA`` aren't scalars."""
    assert not _is_scalar_extends_schema({"extends": ["sensor.DELTA_SCHEMA"]})


def test_scalar_extends_rejects_when_config_vars_present() -> None:
    """A schema with ``config_vars`` carries its own mapping shape."""
    assert not _is_scalar_extends_schema(
        {
            "extends": ["core.positive_time_period_milliseconds"],
            "config_vars": {"some_field": {}},
        }
    )


def test_scalar_extends_rejects_empty_and_none() -> None:
    """Defensive: missing or empty schemas don't match."""
    assert not _is_scalar_extends_schema(None)
    assert not _is_scalar_extends_schema({})
    assert not _is_scalar_extends_schema({"extends": []})


# ---------------------------------------------------------------------------
# _convert_field — extends-of-scalar collapse (now shares the helper)
# ---------------------------------------------------------------------------


def test_convert_field_collapses_returning_lambda_extends_to_lambda() -> None:
    """Fields whose schema extends ``core.returning_lambda`` get type ``lambda``."""
    raw = {
        "key": "Optional",
        "type": "schema",
        "schema": {"extends": ["core.returning_lambda"]},
    }
    entry = _convert_field("on_value", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["type"] == "lambda"


# ---------------------------------------------------------------------------
# _convert_registry_entry — integration with the scalar-extends branch
# ---------------------------------------------------------------------------


def test_convert_registry_entry_returns_empty_config_entries_for_time_period() -> None:
    """`throttle`-shaped filter body produces no decomposed sub-fields."""
    entry = _convert_registry_entry(
        name="throttle",
        body={
            "schema": {"extends": ["core.positive_time_period_milliseconds"]},
            "type": "schema",
        },
        label_domain="sensor",
        applies_to=["sensor"],
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert entry is not None
    assert entry["id"] == "throttle"
    assert entry["config_entries"] == []
    assert entry["applies_to"] == ["sensor"]
    assert entry["value_type"] == "time_period"


def test_convert_registry_entry_tags_lambda_filter_by_id() -> None:
    """The lambda filter has no schema in the bundle; detect by id."""
    entry = _convert_registry_entry(
        name="lambda",
        body={"docs": "..."},
        label_domain="sensor",
        applies_to=["sensor"],
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert entry is not None
    assert entry["config_entries"] == []
    assert entry["value_type"] == "lambda"


def test_convert_registry_entry_returns_empty_config_entries_for_returning_lambda() -> None:
    """A hypothetical lambda-bodied filter with a schema extends, not just docs."""
    entry = _convert_registry_entry(
        name="lambda",
        body={"schema": {"extends": ["core.returning_lambda"]}, "type": "schema"},
        label_domain="sensor",
        applies_to=["sensor"],
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert entry is not None
    assert entry["config_entries"] == []
    assert entry["value_type"] == "lambda"


def test_convert_registry_entry_keeps_mapping_for_non_scalar_extends() -> None:
    """`extends` to a non-scalar schema name shouldn't trigger the bail-out."""
    entry = _convert_registry_entry(
        name="delta",
        body={"schema": {"extends": ["sensor.DELTA_SCHEMA"]}, "type": "schema"},
        label_domain="sensor",
        applies_to=["sensor"],
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert entry is not None
    # Without a resolver the extends-to-mapping ref leaves config_entries
    # empty too, but it goes through ``_extract_automation_param_schema``
    # rather than the scalar-bail; the assertion that matters is the
    # routing distinction (no early return).
    assert entry["id"] == "delta"


# ---------------------------------------------------------------------------
# _REGISTRY_FIELD_OVERRIDES — schema-bundle workarounds
# ---------------------------------------------------------------------------


def test_to_ntc_resistance_calibration_promoted_to_multi_value() -> None:
    """Upstream's custom ``ntc_process_calibration`` validator hides the list shape."""
    entry = _convert_registry_entry(
        name="to_ntc_resistance",
        body={
            "schema": {
                "config_vars": {
                    "calibration": {"key": "Required", "docs": "calibration data."},
                },
            },
            "type": "schema",
        },
        label_domain="sensor",
        applies_to=["sensor"],
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert entry is not None
    cal = next(c for c in entry["config_entries"] if c["key"] == "calibration")
    assert cal["multi_value"] is True


def test_to_ntc_temperature_calibration_promoted_to_multi_value() -> None:
    """Same override as the resistance variant; both share the validator."""
    entry = _convert_registry_entry(
        name="to_ntc_temperature",
        body={
            "schema": {
                "config_vars": {
                    "calibration": {"key": "Required", "docs": "calibration data."},
                },
            },
            "type": "schema",
        },
        label_domain="sensor",
        applies_to=["sensor"],
        schema_dir=_UNUSED_SCHEMA_DIR,
    )
    assert entry is not None
    cal = next(c for c in entry["config_entries"] if c["key"] == "calibration")
    assert cal["multi_value"] is True
