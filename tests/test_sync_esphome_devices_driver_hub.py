"""Pin ``_extract_driver_hubs`` lifting a dependency-bound LED-driver hub + ``requires``."""

from __future__ import annotations

from typing import Any

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _extract_driver_hubs,
    _fold_requires_into_bundles,
    _gpio_number,
)

# Minimal catalog: an output platform that depends on a pin-owning driver hub,
# plus a bus (excluded) for the negative case.
_COMPONENTS: dict[str, dict[str, Any]] = {
    "output.bp5758d": {
        "id": "output.bp5758d",
        "dependencies": ["bp5758d"],
        "config_entries": [
            {"key": "channel", "type": "integer"},
            {"key": "id", "type": "id"},
        ],
    },
    "bp5758d": {
        "id": "bp5758d",
        "category": "misc",
        "config_entries": [
            {"key": "clock_pin", "type": "pin", "required": True},
            {"key": "data_pin", "type": "pin", "required": True},
            {"key": "rgb_current", "type": "string"},
            {"key": "id", "type": "id"},
        ],
    },
    "sensor.bme280_i2c": {
        "id": "sensor.bme280_i2c",
        "dependencies": ["i2c"],
        "config_entries": [{"key": "address", "type": "string"}],
    },
    "i2c": {
        "id": "i2c",
        "category": "bus",
        "config_entries": [
            {"key": "sda", "type": "pin"},
            {"key": "scl", "type": "pin"},
        ],
    },
}


def _outputs() -> list[dict[str, Any]]:
    """Two finalized ``output.bp5758d`` featured entries, as the main pass emits them."""
    return [
        {"id": "output_red", "component_id": "output.bp5758d", "fields": {"channel": 3}},
        {"id": "output_green", "component_id": "output.bp5758d", "fields": {"channel": 2}},
    ]


def test_gpio_number_parses_libretiny_p_pins() -> None:
    """bk72xx ``P26`` resolves to GPIO 26; rtl-style ``PA_0`` stays unparsed."""
    assert _gpio_number("P26") == 26
    assert _gpio_number("p7") == 7
    assert _gpio_number("GPIO5") == 5
    assert _gpio_number("PA_0") is None


def test_lifts_hub_with_locked_pins_and_stamps_requires() -> None:
    """A bp5758d block lifts as a locked hub entry; every output gains ``requires``."""
    featured = _outputs()
    extra, occ = _extract_driver_hubs(
        {"bp5758d": {"clock_pin": "P26", "data_pin": "P24"}}, featured, _COMPONENTS
    )
    assert extra == [
        {
            "id": "bp5758d_hub",
            "component_id": "bp5758d",
            "fields": {
                "clock_pin": {"value": 26, "locked": True},
                "data_pin": {"value": 24, "locked": True},
            },
        }
    ]
    assert occ == {26: "bp5758d", 24: "bp5758d"}
    assert all(e["requires"] == ["bp5758d_hub"] for e in featured)


def test_bus_dependency_is_not_a_driver_hub() -> None:
    """An ``i2c`` bus dep is left to the bus path — never lifted as a driver hub."""
    featured = [{"id": "sensor_x", "component_id": "sensor.bme280_i2c", "fields": {}}]
    extra, occ = _extract_driver_hubs({"i2c": {"sda": "P1", "scl": "P0"}}, featured, _COMPONENTS)
    assert extra == []
    assert occ == {}
    assert "requires" not in featured[0]


def test_absent_hub_block_leaves_outputs_untouched() -> None:
    """No top-level hub block: emit nothing and leave the outputs' ``requires`` unset."""
    featured = _outputs()
    extra, occ = _extract_driver_hubs({}, featured, _COMPONENTS)
    assert extra == []
    assert occ == {}
    assert all("requires" not in e for e in featured)


def test_ambiguous_multi_hub_is_skipped() -> None:
    """Two same-domain hub blocks can't be disambiguated from a finalized output: skip."""
    featured = _outputs()
    config = {
        "bp5758d": [
            {"id": "a", "clock_pin": 1, "data_pin": 2},
            {"id": "b", "clock_pin": 3, "data_pin": 4},
        ]
    }
    extra, _ = _extract_driver_hubs(config, featured, _COMPONENTS)
    assert extra == []
    assert all("requires" not in e for e in featured)


def test_unparsable_hub_pin_skips_hub_and_requires() -> None:
    """A lambda/reference pin the hub can't lock yields no hub and no ``requires``."""
    featured = _outputs()
    extra, _ = _extract_driver_hubs(
        {"bp5758d": {"clock_pin": "!lambda return 1;", "data_pin": "!lambda return 2;"}},
        featured,
        _COMPONENTS,
    )
    assert extra == []
    assert all("requires" not in e for e in featured)


def test_scalar_fields_dont_rescue_a_missing_required_pin() -> None:
    """A liftable scalar must not mask an unparsable required pin (#1728 review)."""
    featured = _outputs()
    extra, _ = _extract_driver_hubs(
        # data_pin is a lambda (dropped), but rgb_current makes fields non-empty.
        {"bp5758d": {"clock_pin": "P26", "data_pin": "!lambda return 2;", "rgb_current": "10mA"}},
        featured,
        _COMPONENTS,
    )
    assert extra == []
    assert all("requires" not in e for e in featured)


def test_fold_prepends_required_hub_ahead_of_members() -> None:
    """A bundle gains its members' required hub (bus then hub), deduped, hub-first."""
    bundles = [{"id": "b", "name": "x", "component_ids": ["output_red", "id_name"]}]
    featured = [
        {"id": "output_red", "component_id": "output.bp5758d", "requires": ["bp5758d_hub"]},
        {"id": "id_name", "component_id": "light.rgbww"},
        {"id": "bp5758d_hub", "component_id": "bp5758d"},
    ]
    _fold_requires_into_bundles(bundles, featured)
    assert bundles[0]["component_ids"] == ["bp5758d_hub", "output_red", "id_name"]


def test_fold_is_a_noop_without_requires() -> None:
    """A bundle whose members carry no ``requires`` is left untouched."""
    bundles = [{"id": "b", "name": "x", "component_ids": ["a", "b"]}]
    featured = [
        {"id": "a", "component_id": "light.binary"},
        {"id": "b", "component_id": "output.gpio"},
    ]
    _fold_requires_into_bundles(bundles, featured)
    assert bundles[0]["component_ids"] == ["a", "b"]


def test_fold_does_not_duplicate_a_prereq_already_in_the_bundle() -> None:
    """A required hub already listed as a member isn't prepended again."""
    bundles = [{"id": "b", "name": "x", "component_ids": ["bp5758d_hub", "output_red"]}]
    featured = [
        {"id": "output_red", "component_id": "output.bp5758d", "requires": ["bp5758d_hub"]},
        {"id": "bp5758d_hub", "component_id": "bp5758d"},
    ]
    _fold_requires_into_bundles(bundles, featured)
    assert bundles[0]["component_ids"] == ["bp5758d_hub", "output_red"]
