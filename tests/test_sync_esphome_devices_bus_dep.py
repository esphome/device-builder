"""Pin ``_extract_bus_deps`` lifting a featured leaf's direct bus dependency + locked pins."""

from __future__ import annotations

from typing import Any

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _collect_bus_dep_refs,
    _extract_bus_deps,
    _extract_fields,
    _find_consumer_block,
    _fold_requires_into_bundles,
    _materialize_bus,
)

# A display that binds an spi bus by catalog dependency, two sensors that share
# an i2c bus, the spi/i2c bus components themselves, and a non-bus dep so the
# category filter is exercised.
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
