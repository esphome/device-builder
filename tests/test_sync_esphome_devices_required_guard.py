"""Pin the guard dropping a featured candidate whose required field can't be represented."""

from __future__ import annotations

from typing import Any

import pytest

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _config_entries_by_key,
    _extract_bus_deps,
    _extract_featured_components,
    _required_pin_keys,
)

_COMPONENTS: dict[str, dict[str, Any]] = {
    "display.st7701s": {
        "config_entries": [
            {
                "key": "dimensions",
                "type": "nested",
                "required": True,
                "config_entries": [
                    {"key": "width", "type": "integer"},
                    {"key": "height", "type": "integer"},
                ],
            },
            {"key": "transform", "type": "nested"},
            {"key": "cs_pin", "type": "pin"},
        ],
    },
    "output.gpio": {
        "config_entries": [{"key": "pin", "type": "pin", "required": True}],
    },
    "light.binary": {
        "config_entries": [
            {"key": "output", "type": "id", "required": True},
            {"key": "restore_mode", "type": "string"},
            {"key": "name", "type": "string"},
        ],
    },
    "display.mipi_spi": {
        "dependencies": ["spi"],
        "config_entries": [
            {"key": "dc_pin", "type": "pin"},
            {"key": "spi_id", "type": "id"},
        ],
    },
    "spi": {
        "category": "bus",
        "config_entries": [
            {"key": "clk_pin", "type": "pin", "required": True},
            {"key": "mosi_pin", "type": "pin"},
            {"key": "id", "type": "id"},
        ],
    },
    "display.epaper_spi": {
        "config_entries": [
            {"key": "model", "type": "string", "required": True},
            {
                "key": "dimensions",
                "type": "nested",
                "required": True,
                "depends_on": "model",
                "depends_on_value_any": ["SSD1683"],
                "config_entries": [
                    {"key": "width", "type": "integer"},
                    {"key": "height", "type": "integer"},
                ],
            },
            {
                "key": "dimensions",
                "type": "nested",
                "depends_on": "model",
                "depends_on_value_any": ["INKPLATE6COLOR"],
                "config_entries": [
                    {"key": "width", "type": "integer"},
                    {"key": "height", "type": "integer"},
                ],
            },
        ],
    },
}


def _display_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "platform": "st7701s",
        "id": "tft_display",
        "dimensions": {"width": 480, "height": 480},
        "cs_pin": 39,
    }
    item.update(overrides)
    return item


def test_unrepresentable_required_field_drops_candidate() -> None:
    """A required field whose value we can't carry drops the entry, not just the field."""
    inline = {"display": [_display_item(dimensions={"width": "${w}", "height": 480})]}
    featured, _, _ = _extract_featured_components(inline, _COMPONENTS)
    assert featured == []


def test_unrepresentable_optional_field_keeps_candidate() -> None:
    """An optional field that fails coercion is skipped silently as before."""
    inline = {"display": [_display_item(transform={"mirror_x": "${flip}"})]}
    featured, _, _ = _extract_featured_components(inline, _COMPONENTS)
    assert len(featured) == 1
    fields = featured[0]["fields"]
    assert "transform" not in fields
    assert fields["cs_pin"] == {"value": 39, "locked": True}


def _epaper_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"platform": "epaper_spi", "model": "SSD1683"}
    item.update(overrides)
    return item


@pytest.mark.parametrize(
    ("model", "dimensions", "kept"),
    [
        pytest.param("SSD1683", {"width": "${w}", "height": 128}, False, id="required-gate"),
        pytest.param("INKPLATE6COLOR", {"width": "${w}", "height": 128}, True, id="optional-gate"),
        pytest.param("Ssd1683", {"width": 128, "height": 296}, True, id="unmatched-spelling"),
        pytest.param("Ssd1683", {"width": "${w}", "height": 296}, False, id="unmatched-closed"),
    ],
)
def test_model_gate_drives_the_drop_guard(model: str, dimensions: dict, kept: bool) -> None:
    """The item's `model` picks the twin the guard reads; unmatched spellings fail closed."""
    inline = {"display": [_epaper_item(model=model, dimensions=dimensions)]}
    featured, _, _ = _extract_featured_components(inline, _COMPONENTS)
    assert bool(featured) is kept


def test_defaulted_discriminator_activates_its_gate() -> None:
    """An omitted discriminator takes its catalog default before gates are read."""
    component = {
        "config_entries": [
            {
                "key": "type",
                "type": "string",
                "default_value": "gpio",
                "options": [
                    {"label": "GPIO", "value": "gpio"},
                    {"label": "SPI", "value": "spi"},
                    {"label": "Ungated", "value": "bare"},
                ],
            },
            {
                "key": "data_pin",
                "type": "pin",
                "required": True,
                "depends_on": "type",
                "depends_on_value_any": ["gpio"],
            },
        ],
    }

    def required_pins(item: dict[str, Any]) -> set[str]:
        return _required_pin_keys(_config_entries_by_key(component, item, "x"))

    assert required_pins({}) == {"data_pin"}
    # A recognised variant excludes the key, whether or not it gates anything
    # itself; an unknown spelling fails closed.
    assert required_pins({"type": "spi"}) == set()
    assert required_pins({"type": "bare"}) == set()
    assert required_pins({"type": "Gpio"}) == {"data_pin"}


def test_dropped_candidate_records_no_occupancy() -> None:
    """A dropped entry's already-coerced pins never pollute the pins block."""
    inline = {"display": [_display_item(dimensions={"width": "${w}", "height": 480})]}
    _, _, occupancy = _extract_featured_components(inline, _COMPONENTS)
    assert occupancy == {}


def test_required_id_ref_is_exempt() -> None:
    """A required id-typed field stays deferred to pass 2 instead of dropping the entry."""
    inline = {
        "output": [{"platform": "gpio", "id": "out1", "pin": 5}],
        "light": [{"platform": "binary", "name": "Backlight", "output": "out1"}],
    }
    featured, _, _ = _extract_featured_components(inline, _COMPONENTS)
    by_component = {entry["component_id"] for entry in featured}
    assert by_component == {"output.gpio", "light.binary"}


def test_consumer_of_dropped_producer_is_pruned() -> None:
    """A consumer whose only preset referenced the dropped producer doesn't survive."""
    inline = {
        "output": [{"platform": "gpio", "id": "out1", "pin": "${relay_pin}"}],
        "light": [{"platform": "binary", "name": "Backlight", "output": "out1"}],
    }
    featured, _, _ = _extract_featured_components(inline, _COMPONENTS)
    assert featured == []


def test_bus_with_unrepresentable_required_field_not_materialized() -> None:
    """A bus lift hits the same guard; the consumer's requires stays unstamped."""
    featured = [
        {
            "id": "my_display",
            "component_id": "display.mipi_spi",
            "fields": {"dc_pin": {"value": 17, "locked": True}, "id": "my_display"},
        }
    ]
    config = {
        "spi": {"id": "lcd_spi", "clk_pin": "${clk}", "mosi_pin": 47},
        "display": [{"platform": "mipi_spi", "id": "my_display", "spi_id": "lcd_spi"}],
    }
    extra, occupancy = _extract_bus_deps(config, featured, _COMPONENTS)
    assert extra == []
    assert occupancy == {}
    assert "requires" not in featured[0]
