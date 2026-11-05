"""Tests for the ``display_format: "hex"`` flag on integer entries.

The upstream schema dump tags i2c-style fields with
``data_type: "hex_uint8_t"`` / ``hex_uint16_t`` / etc. — same shape
as the plain ``uint*_t`` types, but with a "display as hex" intent
that the dashboard's older sync flattened away.

These tests pin the data-type → flag mapping so a future schema
shape change (or a refactor that moves the lookup table) doesn't
silently revert i2c addresses to decimal display, which surfaces
to the user as an unreadable form field
(``119`` instead of ``0x77``) — see issue #410.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import esphome.config_validation as cv
import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _OUTPUT_BODIES_DIR,
    RefinedType,
    _apply_refined_types,
    _collect_bleed_keys,
    _collect_refined_types,
    _convert_field,
    _refined_type_tables,
)


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    """Empty dir for ``_convert_field`` (only used for ``extends`` lookups)."""
    return tmp_path


def test_hex_uint8_t_address_carries_hex_display_format(
    schema_dir: Path,
) -> None:
    """The bme280_i2c.address-shaped raw entry → integer + hex display + 0..255 range.

    Mirrors what
    ``schemas/bme280_i2c.json``'s ``CONFIG_SCHEMA.config_vars.address``
    looks like in the live schema dump.
    """
    raw = {"data_type": "hex_uint8_t", "default": "119", "key": "Optional"}
    entry = _convert_field("address", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "integer"
    assert entry["display_format"] == "hex"
    assert entry["range"] == [0, 255]
    # ``119`` (decimal of ``0x77`` — the BME280's default address)
    # round-trips as the string the upstream schema emits; the
    # frontend's hex-aware renderer formats it on display.
    assert entry["default_value"] == "119"


@pytest.mark.parametrize(
    "data_type, expected_max",
    [
        ("hex_uint8_t", 255),
        ("hex_uint16_t", 65535),
        ("hex_uint32_t", 4294967295),
    ],
)
def test_hex_data_types_set_display_format_and_range(
    schema_dir: Path, data_type: str, expected_max: int
) -> None:
    """
    All sized hex types map to ``display_format: hex`` and matching range.

    ``hex_uint64_t`` is out of scope here — Python ints handle it but JS
    ``Number`` loses precision above ``2**53 - 1`` and the catalog skips
    its ``range``.
    """
    raw = {"data_type": data_type, "key": "Optional"}
    entry = _convert_field("register", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "integer"
    assert entry["display_format"] == "hex"
    assert entry["range"] == [0, expected_max]


def test_hex_uint64_t_sets_display_format_without_range(
    schema_dir: Path,
) -> None:
    """
    64-bit hex is hex-typed but has no range in the catalog.

    JS ``Number``'s 53-bit safe-integer ceiling means a ``[0, 2**64 - 1]``
    upper bound would silently overflow on the frontend. The renderer
    still picks up the hex flag from ``display_format`` so the input
    formats correctly.
    """
    raw = {"data_type": "hex_uint64_t", "key": "Optional"}
    entry = _convert_field("uuid", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "integer"
    assert entry["display_format"] == "hex"
    # Intentionally left unset — see docstring.
    assert entry["range"] is None


def test_plain_uint8_t_does_not_set_hex_display_format(
    schema_dir: Path,
) -> None:
    """
    Plain ``uint8_t`` stays as decimal-display integer.

    Counters, percentages, and similar plain byte fields share the
    ``[0, 255]`` range with i2c addresses but aren't hex-conventional.
    Symmetry check that the flag is per-type and not derived from the
    range alone.
    """
    raw = {"data_type": "uint8_t", "key": "Optional"}
    entry = _convert_field("count", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "integer"
    assert entry["display_format"] is None
    assert entry["range"] == [0, 255]


def test_positive_int_does_not_set_hex_display_format(
    schema_dir: Path,
) -> None:
    """
    ``positive_int`` is a generic non-hex integer.

    Most common integer ``data_type`` in the schema. Pin that the catalog
    stays decimal for the long tail of integer fields.
    """
    raw = {"data_type": "positive_int", "key": "Optional"}
    entry = _convert_field("update_interval", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "integer"
    assert entry["display_format"] is None


def test_string_field_has_no_display_format(
    schema_dir: Path,
) -> None:
    """
    ``display_format`` is meaningful only for INTEGER entries.

    String / pin / boolean entries always serialize it as ``None``.
    """
    raw = {"type": "string", "key": "Optional"}
    entry = _convert_field("ssid", raw, schema_dir)
    assert entry is not None
    assert entry["type"] == "string"
    assert entry["display_format"] is None


def test_hex_int_range_refines_to_hex_integer() -> None:
    """A ``cv.hex_int_range`` chain refines to an integer with hex display."""
    schema = cv.Schema({cv.Required("memory_location"): cv.hex_int_range(min=0x00, max=0x79)})
    refined = _collect_refined_types(SimpleNamespace(config_schema=schema))
    assert refined[("memory_location",)].type == "integer"
    assert refined[("memory_location",)].display_format == "hex"


def test_refined_hex_display_applies_over_string_entries() -> None:
    """The applier stamps type and display_format from a hex refinement."""
    entries = [{"key": "memory_address", "type": "string", "display_format": None}]
    refined = {("memory_address",): RefinedType("integer", display_format="hex")}
    _apply_refined_types(entries, refined)
    assert entries[0]["type"] == "integer"
    assert entries[0]["display_format"] == "hex"


def test_shipped_catalog_micronova_memory_fields_are_hex_integers() -> None:
    """The generated micronova bodies render memory fields as hex integers with bounds."""
    body_path = _OUTPUT_BODIES_DIR / "sensor.micronova.json"
    body = json.loads(body_path.read_text(encoding="utf-8"))
    fan_speed = next(e for e in body["config_entries"] if e["key"] == "fan_speed")
    entries = {e["key"]: e for e in fan_speed["config_entries"]}
    assert entries["memory_location"]["type"] == "integer"
    assert entries["memory_location"]["display_format"] == "hex"
    assert entries["memory_location"]["range"] == [0, 121]
    assert entries["memory_address"]["range"] == [0, 255]


def test_bare_hex_int_refines_to_hex_integer() -> None:
    """A bare ``cv.hex_int`` field (openthread's network key shape) refines to hex."""
    schema = cv.Schema({cv.Optional("network_key"): cv.hex_int})
    refined = _collect_refined_types(SimpleNamespace(config_schema=schema))
    assert refined[("network_key",)].type == "integer"
    assert refined[("network_key",)].display_format == "hex"


def test_hex_stamp_suppressed_on_options_entries() -> None:
    """An options-backed enum keeps decimal option values, no hex stamp."""
    entries = [
        {
            "key": "preamble_polarity",
            "type": "string",
            "display_format": None,
            "options": [{"label": "170", "value": "170"}, {"label": "85", "value": "85"}],
        },
    ]
    refined = {("preamble_polarity",): RefinedType("integer", display_format="hex")}
    _apply_refined_types(entries, refined)
    assert entries[0]["type"] == "integer"
    assert entries[0]["display_format"] is None


def test_missing_refined_type_validator_fails_the_sync() -> None:
    """A registered validator name absent from cv raises instead of silently skipping."""

    class _StubCV:
        pass

    with pytest.raises(SystemExit, match=r"refined-type validator cv\."):
        _refined_type_tables(_StubCV())


def test_hub_refinement_bleed_is_shed_for_platform_builds() -> None:
    """A hub hex refinement is shed for a platform that redefines the key differently."""
    from script.sync_components import introspect_component  # noqa: PLC0415

    introspection = introspect_component("modbus_controller")
    shed = introspection.get("refined_bleed_keys", {}).get("sensor", {})
    assert ("address",) in shed
    # The platform item's ``cv.positive_int`` carries no refinement of its
    # own, so the shed drops the hub's hex outright.
    assert shed[("address",)] is None


def test_shipped_catalog_modbus_platform_address_stays_decimal() -> None:
    """The platform item address keeps decimal display; the hub's hex does not bleed."""
    body_path = _OUTPUT_BODIES_DIR / "sensor.modbus_controller.json"
    body = json.loads(body_path.read_text(encoding="utf-8"))
    entry = next(e for e in body["config_entries"] if e["key"] == "address")
    assert entry["display_format"] is None


def test_shipped_catalog_bundle_typed_hex_fields_gain_display() -> None:
    """Hex-validated fields the bundle types integer now render hex."""
    body = json.loads((_OUTPUT_BODIES_DIR / "sensor.ade7953_i2c.json").read_text(encoding="utf-8"))
    entries = {e["key"]: e for e in body["config_entries"]}
    assert entries["voltage_gain"]["display_format"] == "hex"
    body = json.loads((_OUTPUT_BODIES_DIR / "uponor_smatrix.json").read_text(encoding="utf-8"))
    entries = {e["key"]: e for e in body["config_entries"]}
    assert entries["time_device_address"]["display_format"] == "hex"


def test_refined_hex_display_stamps_integer_entries_without_retype() -> None:
    """An integer-typed entry takes the hex display and keeps its type and range."""
    entries = [{"key": "voltage_gain", "type": "integer", "display_format": None, "range": [0, 9]}]
    refined = {("voltage_gain",): RefinedType("integer", display_format="hex")}
    _apply_refined_types(entries, refined)
    assert entries[0]["type"] == "integer"
    assert entries[0]["display_format"] == "hex"
    assert entries[0]["range"] == [0, 9]


def test_refined_hex_display_never_clobbers_static_stamp() -> None:
    """A ``data_type``-stamped entry keeps its static display format."""
    entries = [{"key": "address", "type": "integer", "display_format": "hex"}]
    refined = {("address",): RefinedType("integer", display_format="octal")}
    _apply_refined_types(entries, refined)
    assert entries[0]["display_format"] == "hex"


def test_integer_branch_suppresses_stamp_on_options_entries() -> None:
    """An options-backed integer enum takes no display stamp."""
    entries = [
        {
            "key": "sync_value",
            "type": "integer",
            "display_format": None,
            "options": [{"label": "170", "value": "170"}],
        },
    ]
    refined = {("sync_value",): RefinedType("integer", display_format="hex")}
    _apply_refined_types(entries, refined)
    assert entries[0]["display_format"] is None


def test_bleed_guard_sees_list_item_paths(collect_bleed: Callable[..., tuple[dict, dict]]) -> None:
    """A hub refinement inside a list item mapping is shed when a platform redefines it."""
    hub = SimpleNamespace(
        config_schema=cv.Schema(
            {cv.Optional("items"): cv.ensure_list(cv.Schema({cv.Optional("addr"): cv.hex_int}))}
        )
    )
    platform = SimpleNamespace(
        config_schema=cv.Schema(
            {cv.Optional("items"): cv.ensure_list(cv.Schema({cv.Optional("addr"): cv.string}))}
        )
    )
    _range_bleed, refined_bleed = collect_bleed(hub, [("sensor", platform)])
    assert ("items", "addr") in refined_bleed.get("sensor", {})


def test_shed_path_substitutes_the_platforms_own_refinement(
    collect_bleed: Callable[..., tuple[dict, dict]],
) -> None:
    """A shed path where the platform refines differently maps to the platform's refinement."""
    hub = SimpleNamespace(config_schema=cv.Schema({cv.Optional("threshold"): cv.hex_int}))
    platform = SimpleNamespace(config_schema=cv.Schema({cv.Optional("threshold"): cv.frequency}))
    _range_bleed, refined_bleed = collect_bleed(hub, [("sensor", platform)])
    substitute = refined_bleed["sensor"][("threshold",)]
    assert substitute is not None
    assert substitute.type == "float_with_unit"


def test_sibling_platform_refinement_is_shed_where_a_domain_redefines_the_key(
    collect_bleed: Callable[..., tuple[dict, dict]],
) -> None:
    """A sibling platform's refinement is shed from a domain that types the key plainly."""
    hub = SimpleNamespace(config_schema=cv.Schema({}))
    climate = SimpleNamespace(config_schema=cv.Schema({cv.Optional("unit"): cv.frequency}))
    sensor = SimpleNamespace(config_schema=cv.Schema({cv.Optional("unit"): cv.string}))
    _range_bleed, refined_bleed = collect_bleed(hub, [("climate", climate), ("sensor", sensor)])
    assert refined_bleed["sensor"] == {("unit",): None}
    assert ("unit",) not in refined_bleed.get("climate", {})


def test_upgraded_sibling_type_is_shed_from_a_stamp_only_platform() -> None:
    """A stamp-only platform field sheds the type a sibling upgrades the merged entry to."""
    from script.sync_components import _upgrade_stamp_only_refinements  # noqa: PLC0415

    hub = SimpleNamespace(config_schema=cv.Schema({}))
    text = SimpleNamespace(
        config_schema=cv.Schema({cv.Optional("value"): cv.Any(cv.returning_lambda, cv.string)})
    )
    number = SimpleNamespace(config_schema=cv.Schema({cv.Optional("value"): cv.float_}))
    merged: dict = {}
    for manifest in (hub, text, number):
        for path, value in _collect_refined_types(manifest).items():
            merged.setdefault(path, value)
    upgraded = _upgrade_stamp_only_refinements(merged, [text, number])
    assert upgraded[("value",)].type == "float"
    _ranges, refined_bleed = _collect_bleed_keys({}, upgraded, [("text", text), ("number", number)])
    assert ("value",) in refined_bleed["text"]
    assert not refined_bleed["text"][("value",)].type


def test_unit_picker_is_not_refined_as_a_unit_coercer() -> None:
    """A validator named like a unit coercer that rejects a unit-suffixed number stays unrefined."""
    from script.sync_components import _refined_types_in_schema  # noqa: PLC0415

    def temperature_unit(value: str) -> str:
        return cv.one_of("°C", "°F", "K")(value)

    def temperature_with_unit(value: str) -> float:
        return cv.temperature(value)

    def bounded_temperature(value: str) -> float:
        return cv.All(cv.temperature, cv.Range(min=5.0))(value)

    schema = cv.Schema(
        {
            cv.Optional("unit_of_measurement"): temperature_unit,
            cv.Optional("target_temperature"): temperature_with_unit,
            cv.Optional("min_temperature"): bounded_temperature,
        }
    )
    refined = _refined_types_in_schema(schema)
    assert ("unit_of_measurement",) not in refined
    assert refined[("target_temperature",)].type == "float_with_unit"
    assert refined[("min_temperature",)].type == "float_with_unit"


def test_shed_refined_bleed_substitutes_and_drops() -> None:
    """The shed keeps unshed paths, substitutes typed platform refinements, drops the rest."""
    from script.sync_components import _shed_refined_bleed  # noqa: PLC0415

    hub_hex = RefinedType("integer", display_format="hex")
    platform_own = RefinedType("float_with_unit", unit_options=["Hz"])
    refined_types = {
        ("kept",): hub_hex,
        ("substituted",): hub_hex,
        ("dropped",): hub_hex,
    }
    shed = _shed_refined_bleed(refined_types, {("substituted",): platform_own, ("dropped",): None})
    assert shed == {("kept",): hub_hex, ("substituted",): platform_own}
