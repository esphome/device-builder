"""Platform-defaulted icon/device_class land under the advanced section."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _convert_field,
)

_UNUSED_SCHEMA_DIR = Path("/unused")
_BODIES_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)


def _load_config_vars(component_id: str) -> dict:
    body = json.loads((_BODIES_DIR / f"{component_id}.json").read_text(encoding="utf-8"))
    return {entry["key"]: entry for entry in body["config_entries"]}


@pytest.mark.parametrize("key", ["icon", "device_class"])
def test_platform_supplied_default_marks_advanced(key: str) -> None:
    raw = {"key": "Optional", "type": "string", "default": "restart"}
    entry = _convert_field(key, raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is True


@pytest.mark.parametrize("key", ["icon", "device_class"])
def test_no_default_keeps_main_form_placement(key: str) -> None:
    raw = {"key": "Optional", "type": "string"}
    entry = _convert_field(key, raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is False


def test_gated_default_also_marks_advanced() -> None:
    raw = {
        "key": "Optional",
        "type": "string",
        "default_with": {"value": "restart", "components": ["mqtt"]},
    }
    entry = _convert_field("device_class", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is True


def test_required_field_stays_on_main_form_despite_default() -> None:
    raw = {"key": "Required", "type": "string", "default": "restart"}
    entry = _convert_field("device_class", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is False


def test_other_defaulted_keys_unaffected() -> None:
    raw = {"key": "Optional", "type": "string", "default": "60s"}
    entry = _convert_field("update_interval", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is False


def test_parent_not_promoted_when_inner_advanced_only_via_platform_default() -> None:
    raw = {
        "key": "Optional",
        "type": "schema",
        "schema": {
            "config_vars": {
                "device_class": {"key": "Optional", "type": "string", "default": "light"}
            },
            "extends": ["binary_sensor._BINARY_SENSOR_SCHEMA"],
        },
    }
    entry = _convert_field("light", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is False
    (inner,) = [e for e in entry["config_entries"] if e["key"] == "device_class"]
    assert inner["advanced"] is True


def test_parent_still_promoted_when_inner_advanced_on_its_own() -> None:
    raw = {
        "key": "Optional",
        "type": "schema",
        "schema": {
            "config_vars": {
                "entity_category": {"key": "Optional", "type": "string", "default": "config"}
            },
        },
    }
    entry = _convert_field("status", raw, _UNUSED_SCHEMA_DIR)
    assert entry is not None
    assert entry["advanced"] is True


def test_restart_button_body_pins_defaulted_fields_advanced() -> None:
    config_vars = _load_config_vars("button.restart")
    assert config_vars["icon"]["default_value"] == "mdi:restart"
    assert config_vars["icon"].get("advanced") is True
    assert config_vars["device_class"]["default_value"] == "restart"
    assert config_vars["device_class"].get("advanced") is True


def test_template_button_body_keeps_undefaulted_fields_prominent() -> None:
    config_vars = _load_config_vars("button.template")
    assert config_vars["icon"].get("default_value") is None
    assert not config_vars["icon"].get("advanced")
    assert config_vars["device_class"].get("default_value") is None
    assert not config_vars["device_class"].get("advanced")


def test_as5600_sub_readings_stay_on_main_form() -> None:
    config_vars = _load_config_vars("sensor.as5600")
    for key in ("magnitude", "raw_position", "status"):
        assert not config_vars[key].get("advanced"), key
    assert config_vars["gain"].get("advanced") is True


def test_wifi_info_spare_address_slots_stay_promoted() -> None:
    config_vars = _load_config_vars("text_sensor.wifi_info")
    ip_inner = {e["key"]: e for e in config_vars["ip_address"]["config_entries"]}
    assert ip_inner["address_0"].get("advanced") is True
