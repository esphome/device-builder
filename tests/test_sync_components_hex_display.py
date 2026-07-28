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

from pathlib import Path
from types import SimpleNamespace

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _convert_field,
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
    import esphome.config_validation as cv  # noqa: PLC0415

    from script.sync_components import _collect_refined_types  # noqa: PLC0415

    schema = cv.Schema({cv.Required("memory_location"): cv.hex_int_range(min=0x00, max=0x79)})
    refined = _collect_refined_types(SimpleNamespace(config_schema=schema))
    assert refined[("memory_location",)].type == "integer"
    assert refined[("memory_location",)].display_format == "hex"


def test_refined_hex_display_applies_over_string_entries() -> None:
    """The applier stamps type and display_format from a hex refinement."""
    from script.sync_components import (  # noqa: PLC0415
        RefinedType,
        _apply_refined_types,
    )

    entries = [{"key": "memory_address", "type": "string", "display_format": None}]
    refined = {("memory_address",): RefinedType("integer", display_format="hex")}
    _apply_refined_types(entries, refined)
    assert entries[0]["type"] == "integer"
    assert entries[0]["display_format"] == "hex"


def test_shipped_catalog_micronova_memory_fields_are_hex_integers() -> None:
    """The generated micronova bodies render memory fields as hex integers with bounds."""
    import json  # noqa: PLC0415

    body_path = (
        Path(__file__).resolve().parent.parent
        / "esphome_device_builder"
        / "definitions"
        / "components"
        / "sensor.micronova.json"
    )
    body = json.loads(body_path.read_text(encoding="utf-8"))
    fan_speed = next(e for e in body["config_entries"] if e["key"] == "fan_speed")
    entries = {e["key"]: e for e in fan_speed["config_entries"]}
    assert entries["memory_location"]["type"] == "integer"
    assert entries["memory_location"]["display_format"] == "hex"
    assert entries["memory_location"]["range"] == [0, 121]
    assert entries["memory_address"]["range"] == [0, 255]
