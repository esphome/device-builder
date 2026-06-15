"""
Per-variant enum options: the new (#16949 bundle) and old (introspection) paths.

The schema bundle dumps a variant enum as ``{value: {"variants": [...]}}``;
``_build_options`` reads it. psram prefers that bundle schema when present and
falls back to ``_psram_config_entries`` introspection when it's absent.
"""

from __future__ import annotations

import esphome.config_validation as cv
from esphome.schema_extractors import SCHEMA_EXTRACT

from esphome_device_builder.models.common import ConfigValueOption
from script.sync_components import (  # type: ignore[import-not-found]
    _apply_psram_options,
    _build_options,
    _psram_static_fields,
    _variant_enum_map,
)


def _fake_variant_enum(by_value: dict[str, list[str]]):
    """Build a variant_filtered_enum-shaped validator returning its map under SCHEMA_EXTRACT."""

    def validator(value: object) -> object:
        return by_value if value is SCHEMA_EXTRACT else value

    return validator


def test_build_options_reads_variant_enum_lowercased() -> None:
    """New bundle shape: each value's ``variants`` become a lowercased option list."""
    options = _build_options(
        {
            "values": {
                "quad": {"variants": ["ESP32", "ESP32S3"]},
                "octal": {"variants": ["ESP32S3"]},
            }
        }
    )
    assert options == [
        {"label": "quad", "value": "quad", "variants": ["esp32", "esp32s3"]},
        {"label": "octal", "value": "octal", "variants": ["esp32s3"]},
    ]


def test_build_options_plain_enum_has_no_variants() -> None:
    """Old enum shape (``{value: null}``) is unchanged — no ``variants`` key."""
    options = _build_options({"values": {"a": None, "b": None}})
    assert options == [{"label": "a", "value": "a"}, {"label": "b", "value": "b"}]


def test_build_options_keeps_docs_label_without_variants() -> None:
    """A docs-only value dict still yields its label and no variants."""
    options = _build_options({"values": {"x": {"docs": "Nice X"}}})
    assert options == [{"label": "Nice X", "value": "x"}]


def test_apply_psram_options_prefers_bundle_schema() -> None:
    """When the bundle already produced psram entries (new format), keep them."""
    bundle = [{"key": "mode", "type": "string", "options": [{"label": "octal", "value": "octal"}]}]
    _apply_psram_options("psram", bundle)
    # Not overwritten by introspection — exactly what the bundle shipped.
    assert bundle == [
        {"key": "mode", "type": "string", "options": [{"label": "octal", "value": "octal"}]}
    ]


def test_apply_psram_options_falls_back_to_introspection() -> None:
    """With no bundle schema (old format), synthesize the structured editor."""
    entries: list[dict] = []
    _apply_psram_options("psram", entries)
    assert {e["key"] for e in entries} >= {"mode", "speed"}
    mode = next(e for e in entries if e["key"] == "mode")
    assert mode["options"][0]["variants"]  # introspection now tags variants too


def test_config_value_option_omits_empty_variants() -> None:
    """``variants`` defaults empty and is stripped on serialize; round-trips when set."""
    assert ConfigValueOption(label="a", value="a").to_dict() == {"label": "a", "value": "a"}
    tagged = ConfigValueOption(label="octal", value="octal", variants=["esp32s3"])
    assert tagged.to_dict() == {"label": "octal", "value": "octal", "variants": ["esp32s3"]}
    assert ConfigValueOption.from_dict(tagged.to_dict()) == tagged


def test_variant_enum_map_reads_schema_extract() -> None:
    """A variant enum's SCHEMA_EXTRACT map is returned verbatim."""
    mode = _fake_variant_enum({"quad": ["ESP32", "ESP32S3"], "octal": ["ESP32S3"]})
    assert _variant_enum_map(mode) == {
        "quad": ["ESP32", "ESP32S3"],
        "octal": ["ESP32S3"],
    }


def test_variant_enum_map_empty_for_non_variant_validator() -> None:
    """A plain validator that rejects the sentinel or returns a non-map yields {}."""
    assert _variant_enum_map(cv.boolean) == {}
    assert _variant_enum_map(lambda _v: "scalar") == {}


def test_psram_static_fields_extracts_new_static_schema() -> None:
    """The new (static CONFIG_SCHEMA) path lowercases variants and flags booleans."""
    schema = {
        cv.Optional("mode"): _fake_variant_enum(
            {"quad": ["ESP32", "ESP32S3"], "octal": ["ESP32S3"]}
        ),
        cv.Optional("enable_ecc", default=False): cv.boolean,
    }
    fields = _psram_static_fields(schema)
    assert fields["mode"]["options"] == {"quad": ["esp32", "esp32s3"], "octal": ["esp32s3"]}
    assert fields["enable_ecc"]["bool"] is True


def test_psram_static_fields_empty_for_old_callable_schema() -> None:
    """An old-style callable CONFIG_SCHEMA isn't statically extractable — fall back."""
    assert _psram_static_fields(lambda _config: None) == {}
