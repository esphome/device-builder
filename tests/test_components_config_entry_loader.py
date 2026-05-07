"""Round-trip tests for ConfigEntry JSON load + materialise.

The catalog ships ConfigEntry shapes as JSON in
``definitions/components.json``. Two helpers convert them into the
in-memory ``ConfigEntry`` model the API serves to the frontend:

- ``_load_config_entry`` reads the JSON dict
- ``_materialise_entry`` resolves platform_defaults and produces the
  per-request copy the API responds with

Every field exposed to the frontend has to make it through both
helpers; pin the round-trip here so any future field addition
either gets covered or lights up CI.
"""

from __future__ import annotations

from esphome_device_builder.controllers.components import (
    _load_config_entry,
    _materialise_entry,
)
from esphome_device_builder.models.common import ConfigEntryType


def test_load_config_entry_propagates_unit_options() -> None:
    """``_load_config_entry`` reads ``unit_options`` from the JSON dict."""
    entry = _load_config_entry(
        {
            "key": "frequency",
            "type": "float_with_unit",
            "label": "Frequency",
            "default_value": 50,
            "unit_options": ["Hz", "mHz", "kHz", "MHz", "GHz"],
        }
    )
    assert entry.type is ConfigEntryType.FLOAT_WITH_UNIT
    assert entry.unit_options == ["Hz", "mHz", "kHz", "MHz", "GHz"]


def test_load_config_entry_unit_options_defaults_to_none() -> None:
    """Entries without ``unit_options`` (the common case) load with ``None``."""
    entry = _load_config_entry(
        {"key": "name", "type": "string", "label": "Name"},
    )
    assert entry.unit_options is None


def test_load_config_entry_drops_non_string_unit_options() -> None:
    """Malformed unit_options entries are filtered out (not propagated as junk)."""
    entry = _load_config_entry(
        {
            "key": "frequency",
            "type": "float_with_unit",
            "label": "Frequency",
            "unit_options": ["Hz", 42, None, "kHz"],
        }
    )
    assert entry.unit_options == ["Hz", "kHz"]


def test_load_config_entry_unit_options_all_filtered_returns_none() -> None:
    """Lists with no string members fold back to ``None``.

    Rather than emitting an empty list — a half-populated picker
    would reach the frontend as a unit-less FLOAT_WITH_UNIT widget.
    """
    entry = _load_config_entry(
        {
            "key": "frequency",
            "type": "float_with_unit",
            "label": "Frequency",
            "unit_options": [42, None, [], {}],
        }
    )
    assert entry.unit_options is None


def test_materialise_entry_preserves_unit_options() -> None:
    """The per-request copy carries ``unit_options`` through to the API response."""
    loaded = _load_config_entry(
        {
            "key": "frequency",
            "type": "float_with_unit",
            "label": "Frequency",
            "default_value": 50,
            "unit_options": ["Hz", "kHz", "MHz"],
        }
    )
    materialised = _materialise_entry(loaded, target_platform="esp32")
    assert materialised.unit_options == ["Hz", "kHz", "MHz"]


def test_materialise_entry_recurses_into_nested_unit_options() -> None:
    """Nested FLOAT_WITH_UNIT entries inside a NESTED parent keep their units."""
    loaded = _load_config_entry(
        {
            "key": "i2c",
            "type": "nested",
            "label": "I2C",
            "config_entries": [
                {
                    "key": "frequency",
                    "type": "float_with_unit",
                    "label": "Frequency",
                    "unit_options": ["Hz", "kHz"],
                }
            ],
        }
    )
    materialised = _materialise_entry(loaded, target_platform=None)
    assert materialised.config_entries is not None
    assert materialised.config_entries[0].unit_options == ["Hz", "kHz"]


def test_load_config_entry_propagates_display_format_hex() -> None:
    """``display_format: "hex"`` survives the JSON → model load (issue #410)."""
    entry = _load_config_entry(
        {
            "key": "address",
            "type": "integer",
            "label": "Address",
            "default_value": "119",
            "range": [0, 255],
            "display_format": "hex",
        }
    )
    assert entry.display_format == "hex"


def test_load_config_entry_display_format_defaults_to_none() -> None:
    """Entries without ``display_format`` (the common case) load with ``None``."""
    entry = _load_config_entry(
        {"key": "count", "type": "integer", "label": "Count"},
    )
    assert entry.display_format is None


def test_load_config_entry_drops_unknown_display_format() -> None:
    """
    Unknown / future variants fold back to ``None``.

    Mirrors the ``_safe_enum`` pattern used for ``pin_mode`` etc.: a
    catalog from a newer release that introduces ``display_format:
    "binary"`` shouldn't reach an older dashboard's renderer as an
    unrecognised string — the renderer falls through to the
    decimal-number default instead.
    """
    entry = _load_config_entry(
        {
            "key": "addr",
            "type": "integer",
            "label": "Address",
            "display_format": "binary",
        }
    )
    assert entry.display_format is None


def test_materialise_entry_preserves_display_format() -> None:
    """The per-request copy carries ``display_format`` through to the API.

    This is the regression Copilot flagged on PR #414: without
    threading the field through ``_materialise_entry`` the flag
    emitted by ``script/sync_components.py`` would be silently
    dropped before reaching the frontend, and the hex hint would
    never apply in the visual editor.
    """
    loaded = _load_config_entry(
        {
            "key": "address",
            "type": "integer",
            "label": "Address",
            "default_value": "119",
            "range": [0, 255],
            "display_format": "hex",
        }
    )
    materialised = _materialise_entry(loaded, target_platform="esp32")
    assert materialised.display_format == "hex"


def test_materialise_entry_recurses_into_nested_display_format() -> None:
    """A hex-typed entry nested inside a NESTED parent stays hex on materialise.

    No catalog entry today places a hex field inside a nested group
    — i2c addresses are flat children of their component — but the
    materialiser is recursive and the field has to flow through the
    same branch that handles every other ConfigEntry attribute, so
    pin the recursion explicitly.
    """
    loaded = _load_config_entry(
        {
            "key": "device",
            "type": "nested",
            "label": "Device",
            "config_entries": [
                {
                    "key": "register",
                    "type": "integer",
                    "label": "Register",
                    "display_format": "hex",
                }
            ],
        }
    )
    materialised = _materialise_entry(loaded, target_platform=None)
    assert materialised.config_entries is not None
    assert materialised.config_entries[0].display_format == "hex"
