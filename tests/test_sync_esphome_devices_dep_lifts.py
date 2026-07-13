"""Pin the dependency-lift repairs: bare hubs, platform hubs, chained buses, named pins."""

from __future__ import annotations

from typing import Any

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _coerce_field_preset,
    _dep_already_featured,
    _extract_bus_deps,
    _extract_driver_hubs,
    _extract_featured_components,
    _is_driver_hub,
    _is_simple_scalar,
    _LiftState,
    _materialize_bus,
    _missing_required_ref,
    _select_block,
)

_COMPONENTS: dict[str, dict[str, Any]] = {
    "switch.tuya": {
        "dependencies": ["tuya"],
        "config_entries": [{"key": "switch_datapoint", "type": "integer"}],
    },
    "tuya": {
        "category": "misc",
        "config_entries": [{"key": "uart_id", "type": "id"}],
        "dependencies": ["uart"],
    },
    "uart": {
        "category": "bus",
        "config_entries": [
            {"key": "tx_pin", "type": "pin"},
            {"key": "rx_pin", "type": "pin"},
            {"key": "baud_rate", "type": "integer"},
            {"key": "id", "type": "id"},
        ],
    },
    "sensor.total_daily_energy": {
        "dependencies": ["time"],
        "config_entries": [{"key": "power_id", "type": "id", "required": True}],
    },
    "time.homeassistant": {
        "category": "time",
        "config_entries": [{"key": "id", "type": "id"}],
    },
}


def test_is_driver_hub_accepts_pinless_bus_attached_hubs() -> None:
    """A hub qualifies by category, not by owning a pin; buses and core stay out."""
    assert _is_driver_hub({"category": "misc", "config_entries": []}) is True
    assert _is_driver_hub({"category": "bus"}) is False
    assert _is_driver_hub({"category": "core"}) is False


def test_select_block_accepts_bare_key() -> None:
    """``tuya:`` with a null body selects as an empty block; an absent key doesn't."""
    assert _select_block({"tuya": None}, "tuya", None) == {}
    assert _select_block({}, "tuya", None) is None
    assert _select_block({"tuya": {"uart_id": "bus"}}, "tuya", None) == {"uart_id": "bus"}


def test_bare_tuya_hub_lifts_and_chains_its_uart() -> None:
    """A bare ``tuya:`` hub materializes fieldless and pulls its uart bus along."""
    featured = [{"id": "sw", "component_id": "switch.tuya", "fields": {"id": "sw"}}]
    config = {
        "tuya": None,
        "uart": {"tx_pin": 1, "rx_pin": 3, "baud_rate": 9600},
        "switch": [{"platform": "tuya", "switch_datapoint": 1}],
    }
    extra, _ = _extract_driver_hubs(config, featured, _COMPONENTS)
    by_cid = {entry["component_id"]: entry for entry in extra}
    assert set(by_cid) == {"tuya", "uart"}
    assert by_cid["tuya"]["requires"] == [by_cid["uart"]["id"]]
    assert featured[0]["requires"][-1] == by_cid["tuya"]["id"]


def test_platform_style_dep_resolves_platform_component() -> None:
    """``time: platform: homeassistant`` lifts as ``time.homeassistant``, not bare ``time``."""
    featured = [
        {
            "id": "energy",
            "component_id": "sensor.total_daily_energy",
            "fields": {"id": "energy", "power_id": "power"},
        }
    ]
    config = {
        "time": {"platform": "homeassistant"},
        "sensor": [{"platform": "total_daily_energy", "power_id": "power"}],
    }
    extra, _ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert [entry["component_id"] for entry in extra] == ["time.homeassistant"]
    assert featured[0]["requires"] == [extra[0]["id"]]


def test_named_pin_alias_locks_without_occupancy() -> None:
    """``TX1``-style aliases lock verbatim; lowercase refs still skip."""
    occupancy: dict[int, str] = {}
    ce = {"key": "tx_pin", "type": "pin"}
    assert _coerce_field_preset(ce, "TX1", "tx_pin", {}, occupancy, "uart") == {
        "value": "TX1",
        "locked": True,
    }
    assert occupancy == {}
    assert _coerce_field_preset(ce, "my_pin_ref", "tx_pin", {}, occupancy, "uart") is None


def test_bare_substitution_is_not_a_simple_scalar() -> None:
    assert _is_simple_scalar("$update_interval") is False
    assert _is_simple_scalar("${update_interval}") is False
    assert _is_simple_scalar("60s") is True


def test_dep_already_featured_matches_platform_style() -> None:
    assert _dep_already_featured("touchscreen", {"touchscreen.ft63x6"}) is True
    assert _dep_already_featured("uart", {"uart"}) is True
    assert _dep_already_featured("uart", {"uart_bus.something"}) is False


def test_missing_required_ref_flags_absent_required_id() -> None:
    component = {"config_entries": [{"key": "output", "type": "id", "required": True}]}
    assert _missing_required_ref(component, {"id": "fan"}) == "output"
    assert _missing_required_ref(component, {"id": "fan", "output": "out1"}) is None


def test_extract_drops_entry_missing_required_ref() -> None:
    """A consumer whose required ref points at a nested sub-id we can't carry is dropped."""
    inline = {"sensor": [{"platform": "total_daily_energy", "power_id": "nested_sub_id"}]}
    featured, _, _ = _extract_featured_components(inline, _COMPONENTS)
    assert featured == []


def test_hub_reuses_already_featured_bus() -> None:
    """A hub whose bus is already featured requires it instead of lifting a duplicate."""
    components = dict(_COMPONENTS)
    components["ads1115"] = {
        "category": "misc",
        "dependencies": ["i2c"],
        "config_entries": [{"key": "address", "type": "integer"}, {"key": "i2c_id", "type": "id"}],
    }
    components["sensor.ads1115"] = {
        "dependencies": ["ads1115"],
        "config_entries": [{"key": "multiplexer", "type": "string"}],
    }
    components["i2c"] = {
        "category": "bus",
        "config_entries": [
            {"key": "sda", "type": "pin"},
            {"key": "scl", "type": "pin"},
            {"key": "id", "type": "id"},
        ],
    }
    featured = [
        {
            "id": "bus_a",
            "component_id": "i2c",
            "fields": {"id": {"value": "bus_a", "locked": True}},
        },
        {"id": "reader", "component_id": "sensor.ads1115", "fields": {"id": "reader"}},
    ]
    config = {
        "i2c": [{"id": "bus_a", "sda": 4, "scl": 5}, {"id": "bus_b", "sda": 21, "scl": 22}],
        "ads1115": {"address": 0x48, "i2c_id": "bus_a"},
        "sensor": [{"platform": "ads1115", "multiplexer": "A0_GND"}],
    }
    extra, _ = _extract_driver_hubs(config, featured, components)
    assert [entry["component_id"] for entry in extra] == ["ads1115"]
    hub = extra[0]
    assert hub["requires"] == ["bus_a"]
    assert hub["fields"]["i2c_id"] == {"value": "bus_a", "locked": True}


def test_chained_and_direct_bus_dep_lift_one_bus() -> None:
    """A hub with both a chained bus dep and a direct one reuses the single bus."""
    components = dict(_COMPONENTS)
    components["modbus"] = {
        "category": "bus",
        "dependencies": ["uart"],
        "config_entries": [{"key": "uart_id", "type": "id"}, {"key": "id", "type": "id"}],
    }
    components["modbus_controller"] = {
        "category": "misc",
        # The AUTO_LOAD closure surfaces uart directly alongside modbus.
        "dependencies": ["modbus", "uart"],
        "config_entries": [
            {"key": "address", "type": "integer"},
            {"key": "modbus_id", "type": "id"},
        ],
    }
    components["sensor.modbus_controller"] = {
        "dependencies": ["modbus_controller"],
        "config_entries": [{"key": "address", "type": "integer"}],
    }
    featured = [
        {"id": "meter", "component_id": "sensor.modbus_controller", "fields": {"id": "meter"}}
    ]
    config = {
        "uart": {"id": "rs485", "tx_pin": 19, "rx_pin": 18},
        "modbus": {"id": "modbus_server", "uart_id": "rs485"},
        "modbus_controller": [{"id": "meter_hub", "address": 1, "modbus_id": "modbus_server"}],
        "sensor": [{"platform": "modbus_controller", "address": 100}],
    }
    extra, _ = _extract_driver_hubs(config, featured, components)
    assert [entry["component_id"] for entry in extra].count("uart") == 1


def test_materialize_bus_accepts_bare_mapping_key() -> None:
    """A bare ``modbus:`` key materializes fieldless via _select_bus_block."""
    components = {"modbus": {"category": "bus", "config_entries": [{"key": "id", "type": "id"}]}}
    state = _LiftState(config={"modbus": None}, components_index=components, used_ids=set())
    entry, local, _ = _materialize_bus("modbus", None, state)
    assert entry is not None
    assert entry["component_id"] == "modbus"
    assert local == "modbus_bus"
