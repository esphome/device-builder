"""Tests for ``_extract_default`` and the ``_convert_field`` end-to-end.

Covers both the resolver and the full conversion pipeline against
real raw-schema fixtures captured from
``script/build_language_schema.py`` post esphome/esphome#16276.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _convert_field,
    _extract_default,
)


def test_unconditional_default_returns_value_and_no_gate() -> None:
    """Plain ``default: "..."`` flows through unchanged."""
    assert _extract_default({"default": "true"}) == (True, None)
    assert _extract_default({"default": "False"}) == (False, None)
    assert _extract_default({"default": "5"}) == ("5", None)


def test_no_default_returns_pair_of_nones() -> None:
    """No ``default`` and no ``default_with`` → ``(None, None)``."""
    assert _extract_default({"key": "Optional"}) == (None, None)


def test_default_with_single_component_returns_value_and_gate() -> None:
    """``default_with`` with one component → gated default."""
    raw = {"default_with": {"value": "True", "components": ["wifi"]}}
    assert _extract_default(raw) == (True, "wifi")


def test_default_with_multi_component_picks_first_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Multi-component ``default_with`` → first component + log.warning.

    No upstream call site uses a list today; pick the first and
    log so the field still gets a default.
    """
    raw = {
        "default_with": {
            "value": "DC_SOURCE",
            "components": ["zigbee", "nrf52"],
        },
    }
    with caplog.at_level(logging.WARNING, logger="sync_components"):
        value, gate = _extract_default(raw, key="power_source")
    assert value == "DC_SOURCE"
    assert gate == "zigbee"
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "power_source" in msg
    assert "zigbee" in msg
    assert "nrf52" in msg


def test_default_with_empty_components_returns_no_gate() -> None:
    """Empty ``components`` → value flows, gate stays None."""
    raw = {"default_with": {"value": "True", "components": []}}
    assert _extract_default(raw) == (True, None)


def test_default_with_takes_precedence_over_default() -> None:
    """``default_with`` wins when both are present."""
    raw = {
        "default": "False",
        "default_with": {"value": "True", "components": ["wifi"]},
    }
    assert _extract_default(raw) == (True, "wifi")


# Real raw entries captured from the patched build_language_schema.py
# against ESPHome's tree. Pasted verbatim so the device-builder side
# has a concrete contract to test against without needing the
# upstream PR merged. The fixtures are the canary if the upstream
# field name changes.

_FIXTURE_SOFTWARE_COEXISTENCE: dict = {
    "default_with": {"value": "True", "components": ["wifi"]},
    "key": "Optional",
    "type": "boolean",
}

_FIXTURE_POWER_SOURCE: dict = {
    "default_with": {"value": "DC_SOURCE", "components": ["nrf52"]},
    "key": "Optional",
    "type": "enum",
    "values": {
        "BATTERY": None,
        "DC_SOURCE": None,
        "EMERGENCY_MAINS_CONST": None,
        "EMERGENCY_MAINS_TRANSF": None,
        "MAINS_SINGLE_PHASE": None,
        "MAINS_THREE_PHASE": None,
        "UNKNOWN": None,
    },
}

_FIXTURE_TX_POWER: dict = {
    "default_without": {"value": "3dBm", "components": ["esp32_hosted"]},
    "key": "Optional",
    "type": "enum",
    "values": {
        "-12": None,
        "-3": None,
        "-6": None,
        "-9": None,
        "0": None,
        "3": None,
        "6": None,
        "9": None,
    },
}


def test_extract_default_software_coexistence_fixture() -> None:
    """Real ``software_coexistence`` raw entry."""
    assert _extract_default(_FIXTURE_SOFTWARE_COEXISTENCE) == (True, "wifi")


def test_extract_default_power_source_fixture() -> None:
    """Real ``power_source`` raw entry — string default."""
    assert _extract_default(_FIXTURE_POWER_SOURCE) == ("DC_SOURCE", "nrf52")


def test_extract_default_tx_power_fixture_skipped_for_now() -> None:
    """``default_without`` returns ``(None, None)`` — inverse-gate follow-up."""
    assert _extract_default(_FIXTURE_TX_POWER) == (None, None)


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    """Empty dir for ``_convert_field`` (only used for ``extends`` lookups)."""
    return tmp_path


def test_convert_field_software_coexistence_carries_gate_and_default(
    schema_dir: Path,
) -> None:
    """``cv.OnlyWith(K, "wifi", default=True)`` → boolean entry, gated."""
    entry = _convert_field("software_coexistence", _FIXTURE_SOFTWARE_COEXISTENCE, schema_dir)
    assert entry is not None
    assert entry["type"] == "boolean"
    assert entry["default_value"] is True
    assert entry["depends_on_component"] == "wifi"
    assert entry["required"] is False


def test_convert_field_power_source_carries_gate_and_string_default(
    schema_dir: Path,
) -> None:
    """OnlyWith enum field with a string default — verifies no bool coercion."""
    entry = _convert_field("power_source", _FIXTURE_POWER_SOURCE, schema_dir)
    assert entry is not None
    assert entry["default_value"] == "DC_SOURCE"
    assert entry["depends_on_component"] == "nrf52"
    option_values = {opt["value"] for opt in entry["options"] or []}
    assert "DC_SOURCE" in option_values
    assert "BATTERY" in option_values


def test_convert_field_tx_power_default_without_no_gate(
    schema_dir: Path,
) -> None:
    """``cv.OnlyWithout`` field → no default, no gate (follow-up)."""
    entry = _convert_field("tx_power", _FIXTURE_TX_POWER, schema_dir)
    assert entry is not None
    assert entry["default_value"] is None
    assert entry["depends_on_component"] is None
    option_values = {opt["value"] for opt in entry["options"] or []}
    assert "3" in option_values


def test_convert_field_bare_trigger_becomes_trigger_type(schema_dir: Path) -> None:
    """A top-level bare ``type: trigger`` field surfaces as TRIGGER, not nested.

    Cover ``open_action`` and similar are action lists the user edits in
    the automation editor; mapping them to an empty ``nested`` group made
    the frontend drop them. They carry no inner ``config_vars``.
    """
    raw = {"key": "Required", "type": "trigger"}
    entry = _convert_field("open_action", raw, schema_dir, top_level=True)
    assert entry is not None
    assert entry["type"] == "trigger"
    assert entry["config_entries"] is None


def test_convert_field_nested_trigger_stays_nested(schema_dir: Path) -> None:
    """A ``type: trigger`` field nested inside another mapping stays nested.

    The ``component_action`` location is ``(component_id, field)``, so it
    can only address a direct component field. Nested trigger fields (e.g.
    ``sprinkler`` valves' ``set_action``) aren't editable and must not be
    promoted to TRIGGER — only ``top_level`` fields are.
    """
    raw = {"key": "Required", "type": "trigger"}
    entry = _convert_field("set_action", raw, schema_dir, top_level=False)
    assert entry is not None
    assert entry["type"] == "nested"


def test_convert_field_trigger_with_inner_config_vars_stays_nested(schema_dir: Path) -> None:
    """A ``type: trigger`` field WITH params keeps the nested mapping (scoped override)."""
    raw = {
        "key": "Optional",
        "type": "trigger",
        "schema": {"config_vars": {"min_length": {"key": "Optional", "type": "integer"}}},
    }
    entry = _convert_field("on_click", raw, schema_dir, top_level=True)
    assert entry is not None
    assert entry["type"] == "nested"


def test_convert_field_unconditional_default_unchanged(schema_dir: Path) -> None:
    """Plain ``cv.Optional(K, default=True)`` flows through with no gate.

    ``retain``'s ``_COMPONENT_GATED_KEYS`` membership applies in
    ``_convert_config_vars``, not ``_convert_field``.
    """
    raw = {"default": "true", "key": "Optional", "type": "boolean"}
    entry = _convert_field("retain", raw, schema_dir)
    assert entry is not None
    assert entry["default_value"] is True
    assert entry["depends_on_component"] is None
