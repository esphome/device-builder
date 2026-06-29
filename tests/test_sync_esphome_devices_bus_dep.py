"""Pin ``_extract_bus_deps`` lifting a featured leaf's direct bus dependency + locked pins."""

from __future__ import annotations

from typing import Any

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _collect_bus_dep_refs,
    _extract_bus_deps,
    _extract_fields,
    _find_consumer_block,
    _fold_requires_into_bundles,
    _is_bus_dep,
    _materialize_bus,
)

# A display that binds an spi bus by catalog dependency, two sensors that share
# an i2c bus, the spi/i2c bus components themselves, a non-bus dep so the category
# filter is exercised, and the platform-style buses (one_wire/canbus) with their
# consumers so the no-top-level-component path is covered.
_COMPONENTS: dict[str, dict[str, Any]] = {
    "display.mipi_spi": {
        "id": "display.mipi_spi",
        "dependencies": ["spi"],
        "config_entries": [
            {"key": "dc_pin", "type": "pin"},
            {"key": "spi_id", "type": "id"},
            {"key": "id", "type": "id"},
        ],
    },
    "spi": {
        "id": "spi",
        "category": "bus",
        "config_entries": [
            {"key": "type", "type": "string"},
            {"key": "clk_pin", "type": "pin", "required": True},
            {"key": "data_pins", "type": "pin"},
            {"key": "mosi_pin", "type": "pin"},
            {"key": "miso_pin", "type": "pin"},
            {"key": "id", "type": "id"},
        ],
    },
    "sensor.bme280": {
        "id": "sensor.bme280",
        "dependencies": ["i2c"],
        "config_entries": [
            {"key": "address", "type": "string"},
            {"key": "i2c_id", "type": "id"},
            {"key": "id", "type": "id"},
        ],
    },
    "sensor.sht3xd": {
        "id": "sensor.sht3xd",
        "dependencies": ["i2c"],
        "config_entries": [
            {"key": "address", "type": "string"},
            {"key": "i2c_id", "type": "id"},
            {"key": "id", "type": "id"},
        ],
    },
    "i2c": {
        "id": "i2c",
        "category": "bus",
        "config_entries": [
            {"key": "scl", "type": "pin"},
            {"key": "sda", "type": "pin"},
            {"key": "id", "type": "id"},
        ],
    },
    "switch.gpio": {
        "id": "switch.gpio",
        "dependencies": ["output"],
        "config_entries": [{"key": "pin", "type": "pin"}],
    },
    # A non-bus dependency so the ``category != "bus"`` filter is exercised
    # (not the absent-component branch).
    "output": {
        "id": "output",
        "category": "misc",
        "config_entries": [{"key": "id", "type": "id"}],
    },
    # The other two mapping-style buses, present only so ``_is_bus_dep`` matches
    # all four (i2c/spi are above).
    "uart": {"id": "uart", "category": "bus"},
    "modbus": {"id": "modbus", "category": "bus"},
    # Platform-style buses: NO top-level ``one_wire`` / ``canbus`` component, only
    # the ``<domain>.<platform>`` entries whose category is the domain name.
    "one_wire.gpio": {
        "id": "one_wire.gpio",
        "category": "one_wire",
        "config_entries": [
            {"key": "pin", "type": "pin"},
            {"key": "id", "type": "id"},
        ],
    },
    "sensor.dallas_temp": {
        "id": "sensor.dallas_temp",
        "dependencies": ["one_wire"],
        "config_entries": [
            {"key": "address", "type": "string"},
            {"key": "one_wire_id", "type": "id"},
            {"key": "id", "type": "id"},
        ],
    },
    "canbus.esp32_can": {
        "id": "canbus.esp32_can",
        "category": "canbus",
        "config_entries": [
            {"key": "tx_pin", "type": "pin"},
            {"key": "rx_pin", "type": "pin"},
            {"key": "can_id", "type": "string"},
            {"key": "id", "type": "id"},
        ],
    },
    "sensor.canbus_bms": {
        "id": "sensor.canbus_bms",
        "dependencies": ["canbus"],
        "config_entries": [
            {"key": "canbus_id", "type": "id"},
            {"key": "id", "type": "id"},
        ],
    },
}


def _dallas(local_id: str = "sensor_dallas_temp_1") -> dict[str, Any]:
    """Return a finalized ``sensor.dallas_temp`` entry (its ``one_wire_id`` ref dropped)."""
    return {
        "id": local_id,
        "component_id": "sensor.dallas_temp",
        "fields": {"id": local_id},
    }


def _display() -> dict[str, Any]:
    """Return a finalized ``display.mipi_spi`` entry (its ``spi_id`` ref already dropped)."""
    return {
        "id": "my_display",
        "component_id": "display.mipi_spi",
        "fields": {"dc_pin": {"value": 17, "locked": True}, "id": "my_display"},
    }


def _spi_block(spi_id: str = "display_spi") -> dict[str, Any]:
    return {
        "type": "octal",
        "id": spi_id,
        "clk_pin": "GPIO21",
        "data_pins": ["GPIO6", "GPIO7", "GPIO15"],
    }


def test_lifts_single_spi_bus_and_stamps_requires() -> None:
    """A display→spi dep lifts the spi block with locked clk/data pins; display gains requires."""
    featured = [_display()]
    config = {
        "spi": _spi_block(),
        "display": [{"platform": "mipi_spi", "id": "my_display", "spi_id": "display_spi"}],
    }
    extra, occ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert len(extra) == 1
    bus = extra[0]
    assert bus["component_id"] == "spi"
    assert bus["id"] == "display_spi"
    assert bus["fields"]["clk_pin"] == {"value": 21, "locked": True}
    assert bus["fields"]["data_pins"] == {"value": [6, 7, 15], "locked": True}
    assert bus["fields"]["id"] == {"value": "display_spi", "locked": True}
    assert featured[0]["requires"] == ["display_spi"]
    assert set(occ) == {21, 6, 7, 15}


def test_two_consumers_share_one_bus() -> None:
    """Two sensors on one i2c lift a single bus entry; both gain the same requires."""
    featured = [
        {"id": "temp", "component_id": "sensor.bme280", "fields": {"id": "temp"}},
        {"id": "humid", "component_id": "sensor.sht3xd", "fields": {"id": "humid"}},
    ]
    config = {
        "i2c": {"id": "bus", "scl": "GPIO5", "sda": "GPIO4"},
        "sensor": [
            {"platform": "bme280", "id": "temp"},
            {"platform": "sht3xd", "id": "humid"},
        ],
    }
    extra, occ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert [e["component_id"] for e in extra] == ["i2c"]
    assert extra[0]["id"] == "bus"
    assert featured[0]["requires"] == ["bus"]
    assert featured[1]["requires"] == ["bus"]
    assert set(occ) == {5, 4}


def test_multi_bus_disambiguates_via_bus_id() -> None:
    """Two i2c buses + two sensors pinning distinct ``i2c_id`` lift both, each wired right."""
    featured = [
        {"id": "temp", "component_id": "sensor.bme280", "fields": {"id": "temp"}},
        {"id": "humid", "component_id": "sensor.sht3xd", "fields": {"id": "humid"}},
    ]
    config = {
        "i2c": [
            {"id": "bus_a", "scl": "GPIO5", "sda": "GPIO4"},
            {"id": "bus_b", "scl": "GPIO22", "sda": "GPIO21"},
        ],
        "sensor": [
            {"platform": "bme280", "id": "temp", "i2c_id": "bus_a"},
            {"platform": "sht3xd", "id": "humid", "i2c_id": "bus_b"},
        ],
    }
    extra, _ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert {e["id"] for e in extra} == {"bus_a", "bus_b"}
    assert featured[0]["requires"] == ["bus_a"]
    assert featured[1]["requires"] == ["bus_b"]


def test_bus_already_featured_is_not_relifted() -> None:
    """A bus present as a featured entry is skipped (no double-lift, no requires stamp)."""
    featured = [
        {"id": "bus", "component_id": "i2c", "fields": {"scl": {"value": 5, "locked": True}}},
        {"id": "temp", "component_id": "sensor.bme280", "fields": {"id": "temp"}},
    ]
    config = {
        "i2c": {"id": "bus", "scl": "GPIO5", "sda": "GPIO4"},
        "sensor": [{"platform": "bme280", "id": "temp"}],
    }
    extra, occ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert extra == []
    assert occ == {}
    assert "requires" not in featured[1]


def test_absent_bus_block_is_a_graceful_noop() -> None:
    """No ``spi:`` block to lift: emit nothing and leave the display's requires unset."""
    featured = [_display()]
    config = {"display": [{"platform": "mipi_spi", "id": "my_display"}]}
    extra, occ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert extra == []
    assert occ == {}
    assert "requires" not in featured[0]


def test_ambiguous_multi_bus_without_ref_is_skipped() -> None:
    """Two unmarked spi blocks and no ``spi_id`` can't be disambiguated: lift nothing."""
    featured = [_display()]
    config = {
        "spi": [_spi_block("bus_a"), _spi_block("bus_b")],
        "display": [{"platform": "mipi_spi", "id": "my_display"}],
    }
    extra, _ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert extra == []
    assert "requires" not in featured[0]


def test_non_bus_dependency_is_ignored() -> None:
    """A featured leaf whose only dep is a non-bus component lifts nothing."""
    featured = [{"id": "sw", "component_id": "switch.gpio", "fields": {"id": "sw"}}]
    config = {"switch": [{"platform": "gpio", "id": "sw"}]}
    consumers, ordered_refs = _collect_bus_dep_refs(config, featured, _COMPONENTS)
    assert consumers == []
    assert ordered_refs == []


def test_requires_is_merged_not_overwritten() -> None:
    """A consumer that already needs a hub keeps it and gains the bus, in order."""
    featured = [
        {
            "id": "temp",
            "component_id": "sensor.bme280",
            "fields": {"id": "temp"},
            "requires": ["some_hub"],
        }
    ]
    config = {
        "i2c": {"id": "bus", "scl": "GPIO5", "sda": "GPIO4"},
        "sensor": [{"platform": "bme280", "id": "temp"}],
    }
    _extract_bus_deps(config, featured, _COMPONENTS)
    assert featured[0]["requires"] == ["some_hub", "bus"]


def test_lifted_bus_folds_into_bundle_ahead_of_consumer() -> None:
    """After lifting, the bus folds into the display's bundle as the first member."""
    featured = [_display()]
    config = {
        "spi": _spi_block(),
        "display": [{"platform": "mipi_spi", "id": "my_display", "spi_id": "display_spi"}],
    }
    bundles = [{"id": "b", "name": "x", "component_ids": ["my_display"]}]
    _extract_bus_deps(config, featured, _COMPONENTS)
    _fold_requires_into_bundles(bundles, featured)
    assert bundles[0]["component_ids"] == ["display_spi", "my_display"]


def test_find_consumer_block_matches_by_id_when_platform_repeats() -> None:
    """Two blocks of one platform disambiguate by sanitized id matching the entry id."""
    config = {
        "display": [
            {"platform": "mipi_spi", "id": "left", "spi_id": "bus_a"},
            {"platform": "mipi_spi", "id": "right", "spi_id": "bus_b"},
        ]
    }
    entry = {"id": "right", "component_id": "display.mipi_spi", "fields": {}}
    block = _find_consumer_block(config, entry)
    assert block is not None
    assert block["spi_id"] == "bus_b"


def test_list_pin_field_locks_and_records_occupancy() -> None:
    """A list-valued ``data_pins`` locks as a list and records every GPIO it occupies."""
    occ: dict[int, str] = {}
    fields = _extract_fields(
        {"id": "x", "data_pins": ["GPIO6", "GPIO7", "GPIO15"]}, _COMPONENTS["spi"], occ, "spi"
    )
    assert fields is not None
    assert fields["data_pins"] == {"value": [6, 7, 15], "locked": True}
    assert set(occ) == {6, 7, 15}


def test_octal_spi_materializes_both_clk_and_data_pins() -> None:
    """End-to-end guard: an octal spi block locks clk_pin and the data_pins list."""
    entry, local, occ = _materialize_bus("spi", None, {"spi": _spi_block()}, _COMPONENTS, set())
    assert entry is not None
    assert local == "display_spi"
    assert entry["fields"]["clk_pin"] == {"value": 21, "locked": True}
    assert entry["fields"]["data_pins"] == {"value": [6, 7, 15], "locked": True}
    assert set(occ) == {21, 6, 7, 15}


def test_is_bus_dep_matches_all_known_buses() -> None:
    """All six ESPHome buses match, both styles; non-bus deps do not."""
    for dep in ("i2c", "spi", "uart", "modbus", "one_wire", "canbus"):
        assert _is_bus_dep(dep, _COMPONENTS) is True
    for dep in ("output", "esp32", "sensor"):
        assert _is_bus_dep(dep, _COMPONENTS) is False


def test_lifts_platform_style_one_wire_without_id() -> None:
    """A dallas_temp→one_wire dep lifts the gpio bus (pin locked, no id) and stamps requires."""
    featured = [_dallas()]
    config = {
        "one_wire": [{"platform": "gpio", "pin": "GPIO4"}],
        "sensor": [{"platform": "dallas_temp", "id": "sensor_dallas_temp_1"}],
    }
    extra, occ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert len(extra) == 1
    bus = extra[0]
    assert bus["component_id"] == "one_wire.gpio"
    assert bus["id"] == "one_wire_bus"
    assert bus["fields"]["pin"] == {"value": 4, "locked": True}
    assert "id" not in bus["fields"]
    assert featured[0]["requires"] == ["one_wire_bus"]
    assert set(occ) == {4}


def test_two_consumers_share_one_platform_bus() -> None:
    """Two dallas_temp sensors on one one_wire bus lift a single entry; both gain requires."""
    featured = [_dallas("d1"), _dallas("d2")]
    config = {
        "one_wire": [{"platform": "gpio", "pin": "GPIO4"}],
        "sensor": [
            {"platform": "dallas_temp", "id": "d1"},
            {"platform": "dallas_temp", "id": "d2"},
        ],
    }
    extra, occ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert [e["component_id"] for e in extra] == ["one_wire.gpio"]
    assert featured[0]["requires"] == ["one_wire_bus"]
    assert featured[1]["requires"] == ["one_wire_bus"]
    assert set(occ) == {4}


def test_platform_bus_multi_disambiguates_and_locks_explicit_id() -> None:
    """Two one_wire buses + sensors pinning distinct ``one_wire_id`` lift both, id locked."""
    featured = [_dallas("d1"), _dallas("d2")]
    config = {
        "one_wire": [
            {"platform": "gpio", "id": "bus_a", "pin": "GPIO4"},
            {"platform": "gpio", "id": "bus_b", "pin": "GPIO5"},
        ],
        "sensor": [
            {"platform": "dallas_temp", "id": "d1", "one_wire_id": "bus_a"},
            {"platform": "dallas_temp", "id": "d2", "one_wire_id": "bus_b"},
        ],
    }
    extra, _ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert {e["id"] for e in extra} == {"bus_a", "bus_b"}
    by_id = {e["id"]: e for e in extra}
    assert by_id["bus_a"]["fields"]["id"] == {"value": "bus_a", "locked": True}
    assert featured[0]["requires"] == ["bus_a"]
    assert featured[1]["requires"] == ["bus_b"]


def test_lifts_platform_style_canbus() -> None:
    """A leaf→canbus dep lifts the esp32_can bus with tx/rx pins locked."""
    featured = [{"id": "bms", "component_id": "sensor.canbus_bms", "fields": {"id": "bms"}}]
    config = {
        "canbus": [{"platform": "esp32_can", "tx_pin": "GPIO5", "rx_pin": "GPIO4"}],
        "sensor": [{"platform": "canbus_bms", "id": "bms"}],
    }
    extra, occ = _extract_bus_deps(config, featured, _COMPONENTS)
    assert len(extra) == 1
    assert extra[0]["component_id"] == "canbus.esp32_can"
    assert extra[0]["fields"]["tx_pin"] == {"value": 5, "locked": True}
    assert extra[0]["fields"]["rx_pin"] == {"value": 4, "locked": True}
    assert featured[0]["requires"] == ["canbus_bus"]
    assert set(occ) == {5, 4}


def test_materialize_platform_bus_direct() -> None:
    """Direct call: an id-less one_wire gpio block resolves the platform component."""
    entry, local, occ = _materialize_bus(
        "one_wire", None, {"one_wire": [{"platform": "gpio", "pin": "GPIO4"}]}, _COMPONENTS, set()
    )
    assert entry is not None
    assert entry["component_id"] == "one_wire.gpio"
    assert local == "one_wire_bus"
    assert entry["fields"]["pin"] == {"value": 4, "locked": True}
    assert "id" not in entry["fields"]
    assert set(occ) == {4}


def test_platform_bus_folds_into_bundle_ahead_of_consumer() -> None:
    """After lifting, the one_wire bus folds into the bundle as the first member."""
    featured = [_dallas()]
    config = {
        "one_wire": [{"platform": "gpio", "pin": "GPIO4"}],
        "sensor": [{"platform": "dallas_temp", "id": "sensor_dallas_temp_1"}],
    }
    bundles = [{"id": "b", "name": "x", "component_ids": ["sensor_dallas_temp_1"]}]
    _extract_bus_deps(config, featured, _COMPONENTS)
    _fold_requires_into_bundles(bundles, featured)
    assert bundles[0]["component_ids"] == ["one_wire_bus", "sensor_dallas_temp_1"]
