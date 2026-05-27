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
    _dedupe_filters,
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
