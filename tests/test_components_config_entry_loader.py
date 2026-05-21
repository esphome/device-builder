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
    _load_component,
    _load_config_entry,
    _materialise_entry,
)
from esphome_device_builder.models.common import ConfigEntryType, RequiredGroupKind


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


def test_load_config_entry_propagates_supported_platforms() -> None:
    """``_load_config_entry`` round-trips ``supported_platforms`` from JSON.

    Without this the per-field platform-gating signal that the sync
    script writes into ``components.json`` would be silently dropped
    when the catalog is loaded into the runtime model — the frontend
    would never see a gated field as gated. (Caught by Copilot review
    on PR #423.)
    """
    entry = _load_config_entry(
        {
            "key": "psram",
            "type": "string",
            "label": "PSRAM",
            "supported_platforms": ["esp32"],
        }
    )
    assert entry.supported_platforms == ["esp32"]


def test_load_config_entry_supported_platforms_defaults_to_empty_list() -> None:
    """A missing ``supported_platforms`` key parses as the empty-list default.

    Empty list is the wire representation of "no platform restriction"
    — the frontend's render filter treats empty / missing as a no-op.
    """
    entry = _load_config_entry(
        {
            "key": "free",
            "type": "string",
            "label": "Free",
        }
    )
    assert entry.supported_platforms == []


def test_materialise_entry_carries_supported_platforms() -> None:
    """``_materialise_entry`` forwards ``supported_platforms`` to the output.

    The materialiser strips ``platform_defaults`` (a sync-time
    implementation detail) but ``supported_platforms`` is the wire
    contract for the FE form filter — it must round-trip.
    """
    loaded = _load_config_entry(
        {
            "key": "fragmentation",
            "type": "string",
            "label": "Fragmentation",
            "supported_platforms": ["esp32", "esp8266"],
        }
    )
    materialised = _materialise_entry(loaded, target_platform="esp32")
    assert materialised.supported_platforms == ["esp32", "esp8266"]


def test_materialise_entry_carries_supported_platforms_through_nested() -> None:
    """Nested entries' ``supported_platforms`` survive the recursion."""
    loaded = _load_config_entry(
        {
            "key": "diagnostics",
            "type": "nested",
            "label": "Diagnostics",
            "config_entries": [
                {
                    "key": "psram",
                    "type": "string",
                    "label": "PSRAM",
                    "supported_platforms": ["esp32"],
                }
            ],
        }
    )
    materialised = _materialise_entry(loaded, target_platform="esp32")
    assert materialised.config_entries is not None
    assert materialised.config_entries[0].supported_platforms == ["esp32"]


def test_load_config_entry_propagates_group() -> None:
    """``group`` rides through the JSON load for ``cv.Inclusive`` fields (issue #924)."""
    entry = _load_config_entry(
        {
            "key": "bit0_high",
            "type": "time_period",
            "label": "Bit 0 high",
            "group": "custom",
        }
    )
    assert entry.group == "custom"


def test_load_config_entry_group_defaults_to_none() -> None:
    """Entries without ``group`` (the common case) load with ``None``."""
    entry = _load_config_entry(
        {"key": "ssid", "type": "string", "label": "SSID"},
    )
    assert entry.group is None


def test_load_config_entry_required_groups_round_trips() -> None:
    """Nested ``required_groups`` parse into typed ``RequiredGroup`` objects."""
    entry = _load_config_entry(
        {
            "key": "eap",
            "type": "nested",
            "label": "EAP",
            "required_groups": [
                {"kind": "at_least_one", "keys": ["identity", "certificate"]},
            ],
        }
    )
    assert len(entry.required_groups) == 1
    assert entry.required_groups[0].kind is RequiredGroupKind.AT_LEAST_ONE
    assert entry.required_groups[0].keys == ["identity", "certificate"]


def test_load_config_entry_drops_required_groups_with_unknown_kind() -> None:
    """
    Unknown ``kind`` values fold away — same fail-soft policy as ``_safe_enum``.

    A catalog from a future release that introduces a fifth
    cardinality validator must not crash an older dashboard's
    loader; the offending entry is dropped so the rest of the
    constraint list still reaches the frontend.
    """
    entry = _load_config_entry(
        {
            "key": "eap",
            "type": "nested",
            "label": "EAP",
            "required_groups": [
                {"kind": "at_least_one", "keys": ["a"]},
                {"kind": "future_variant", "keys": ["b"]},
            ],
        }
    )
    assert [g.kind for g in entry.required_groups] == [RequiredGroupKind.AT_LEAST_ONE]


def test_load_config_entry_drops_required_groups_with_empty_keys() -> None:
    """A group with no string keys is meaningless and gets dropped."""
    entry = _load_config_entry(
        {
            "key": "x",
            "type": "nested",
            "label": "X",
            "required_groups": [
                {"kind": "exactly_one", "keys": []},
                {"kind": "exactly_one", "keys": [42, None]},
                {"kind": "exactly_one"},  # missing keys
            ],
        }
    )
    assert entry.required_groups == []


def test_load_config_entry_skips_required_groups_with_non_dict_items() -> None:
    """Non-dict items in the list are ignored without breaking the rest."""
    entry = _load_config_entry(
        {
            "key": "x",
            "type": "nested",
            "label": "X",
            "required_groups": [
                "garbage",
                ["also", "garbage"],
                {"kind": "at_least_one", "keys": ["good"]},
            ],
        }
    )
    assert [g.kind for g in entry.required_groups] == [RequiredGroupKind.AT_LEAST_ONE]


def test_materialise_entry_carries_group_and_required_groups() -> None:
    """The per-request copy keeps both new fields visible to the frontend."""
    loaded = _load_config_entry(
        {
            "key": "eap",
            "type": "nested",
            "label": "EAP",
            "required_groups": [
                {"kind": "at_least_one", "keys": ["identity", "certificate"]},
            ],
            "config_entries": [
                {
                    "key": "certificate",
                    "type": "string",
                    "label": "Certificate",
                    "group": "cert_and_key",
                },
            ],
        }
    )
    materialised = _materialise_entry(loaded, target_platform="esp32")
    assert len(materialised.required_groups) == 1
    assert materialised.required_groups[0].kind is RequiredGroupKind.AT_LEAST_ONE
    assert materialised.config_entries is not None
    assert materialised.config_entries[0].group == "cert_and_key"


def test_load_component_reads_top_level_required_groups() -> None:
    """Component-level ``required_groups`` (issue #924) round-trip from JSON."""
    component = _load_component(
        {
            "id": "light.esp32_rmt_led_strip",
            "name": "ESP32 RMT LED Strip",
            "description": "",
            "category": "light",
            "required_groups": [
                {"kind": "exactly_one", "keys": ["chipset", "bit0_high"]},
            ],
            "config_entries": [],
        }
    )
    assert len(component.required_groups) == 1
    assert component.required_groups[0].kind is RequiredGroupKind.EXACTLY_ONE
    assert component.required_groups[0].keys == ["chipset", "bit0_high"]


def test_load_component_required_groups_defaults_to_empty_list() -> None:
    """A component without ``required_groups`` parses as the empty-list default."""
    component = _load_component(
        {
            "id": "wifi",
            "name": "Wi-Fi",
            "description": "",
            "category": "core",
            "config_entries": [],
        }
    )
    assert component.required_groups == []
