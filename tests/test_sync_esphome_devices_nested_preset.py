"""Pin the data-only nested pass-through (``dimensions`` / ``data_pins`` / ``init_sequence``)."""

from __future__ import annotations

from typing import Any

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _coerce_field_preset,
    _extract_featured_components,
)

_DIMENSIONS_CE: dict[str, Any] = {
    "key": "dimensions",
    "type": "nested",
    "required": True,
    "config_entries": [
        {"key": "width", "type": "integer"},
        {"key": "height", "type": "integer"},
    ],
}
_DATA_PINS_CE: dict[str, Any] = {"key": "data_pins", "type": "nested", "required": True}
_INIT_SEQUENCE_CE: dict[str, Any] = {"key": "init_sequence", "type": "string", "multi_value": True}


def _coerce(
    config_entry: dict[str, Any],
    value: Any,
    occupancy: dict[int, str] | None = None,
) -> Any:
    return _coerce_field_preset(
        config_entry,
        value,
        config_entry["key"],
        {"id": "tft_display"},
        occupancy if occupancy is not None else {},
        "display.st7701s",
    )


def test_nested_dict_locks_verbatim() -> None:
    """A data-only nested dict rides through as a locked preset."""
    value = {"width": 480, "height": 480}
    assert _coerce(_DIMENSIONS_CE, value) == {"value": value, "locked": True}


def test_list_on_multi_value_string_entry_locks() -> None:
    """A list on a ``multi_value`` scalar entry (``init_sequence``) locks verbatim."""
    value = [1, [0xFF, 0x77, 0x01, 0x00, 0x00, 0x10], [0xCD, 0x00]]
    assert _coerce(_INIT_SEQUENCE_CE, value) == {"value": value, "locked": True}


def test_list_on_plain_scalar_entry_is_skipped() -> None:
    """Without ``multi_value``, a list on a scalar-typed entry is rejected."""
    assert _coerce({"key": "rotation", "type": "string"}, [0, 90]) is None


def test_dict_on_scalar_typed_entry_is_skipped() -> None:
    """A dict only passes on a ``nested``-typed entry."""
    assert _coerce({"key": "rotation", "type": "string"}, {"angle": 90}) is None
    assert _coerce(_INIT_SEQUENCE_CE, {"delay": 1}) is None


def test_data_pins_tree_records_gpio_occupancy() -> None:
    """Every GPIO leaf of a pin-group field lands in the occupancy map, GPIO 0 included."""
    occupancy: dict[int, str] = {}
    value = {"red": [11, 12, 0], "green": [8, 20], "blue": [4]}
    assert _coerce(_DATA_PINS_CE, value, occupancy) == {"value": value, "locked": True}
    assert set(occupancy) == {0, 4, 8, 11, 12, 20}
    assert occupancy[0] == "tft_display"


def test_non_pin_named_tree_records_no_occupancy() -> None:
    """Integer leaves of a non-pin field (``dimensions``) never claim GPIOs."""
    occupancy: dict[int, str] = {}
    _coerce(_DIMENSIONS_CE, {"width": 480, "height": 480}, occupancy)
    assert occupancy == {}


def test_tree_with_template_leaf_is_skipped() -> None:
    """A ``${...}`` substitution anywhere in the tree rejects the whole preset."""
    assert _coerce(_DATA_PINS_CE, {"red": ["${red_pin}"], "blue": [4]}) is None


def test_tree_with_placeholder_leaf_is_skipped() -> None:
    """An upstream fill-me-in sentinel anywhere in the tree rejects the preset."""
    assert _coerce(_DIMENSIONS_CE, {"width": "(FILL IN WIDTH)", "height": 480}) is None


def test_tree_under_id_bearing_entry_is_skipped() -> None:
    """A nested entry declaring an id-typed child can carry refs we can't remap."""
    ce = {
        "key": "clk",
        "type": "nested",
        "config_entries": [{"key": "oscillator_id", "type": "id"}],
    }
    assert _coerce(ce, {"pin": 5}) is None


def test_tree_with_id_keys_is_skipped() -> None:
    """A value carrying ``id`` / ``*_id`` keys is rejected even without catalog typing."""
    assert _coerce(_DATA_PINS_CE, {"output_id": "backlight"}) is None
    assert _coerce(_DATA_PINS_CE, {"group": {"id": "tft"}}) is None


def test_empty_container_is_skipped() -> None:
    """Empty dicts/lists carry no data worth locking."""
    assert _coerce(_DIMENSIONS_CE, {}) is None
    assert _coerce(_INIT_SEQUENCE_CE, []) is None


def test_display_item_extracts_complete_nested_presets() -> None:
    """An st7701s-shaped inline item keeps its required nested fields end to end."""
    index = {
        "display.st7701s": {
            "config_entries": [
                _DIMENSIONS_CE,
                _DATA_PINS_CE,
                _INIT_SEQUENCE_CE,
                {"key": "cs_pin", "type": "pin"},
            ]
        }
    }
    inline = {
        "display": [
            {
                "platform": "st7701s",
                "id": "tft_display",
                "dimensions": {"width": 480, "height": 480},
                "data_pins": {"red": [11], "green": [8], "blue": [4]},
                "init_sequence": [1, [0xCD, 0x00]],
                "cs_pin": 39,
            }
        ]
    }
    featured, _, occupancy = _extract_featured_components(inline, index)
    assert len(featured) == 1
    fields = featured[0]["fields"]
    assert fields["dimensions"] == {"value": {"width": 480, "height": 480}, "locked": True}
    assert fields["data_pins"]["value"] == {"red": [11], "green": [8], "blue": [4]}
    assert fields["init_sequence"] == {"value": [1, [0xCD, 0x00]], "locked": True}
    assert set(occupancy) == {4, 8, 11, 39}
