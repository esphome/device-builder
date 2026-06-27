"""
Tests for ``_extract_featured_components`` in ``script/sync_esphome_devices.py``.

Focuses on the explicit-fields contract: every emitted featured-component
entry must carry ``fields.id`` and, for HA entity domains, ``fields.name``
— so the imported manifests are self-contained and the runtime never
has to auto-derive these from the local id / display name.

Also covers the safety filters that drop upstream items the dashboard
can't usefully surface — placeholder addresses, lambda-driven
templates, cross-component id references — without polluting the
synthesized ``pins[]`` block with orphan GPIO labels.
"""

from __future__ import annotations

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _extract_expander_hubs,
    _extract_featured_components,
)

# Minimal fake components index — only the keys the extractor reads
# (``config_entries[*].key`` / ``type``). The pin entries make the
# extractor accept the values as ``locked`` presets.
_INDEX = {
    "binary_sensor.gpio": {"config_entries": [{"key": "pin", "type": "pin"}]},
    "output.gpio": {"config_entries": [{"key": "pin", "type": "pin"}]},
    "sensor.dht": {
        "config_entries": [
            {"key": "pin", "type": "pin"},
            {"key": "model", "type": "string"},
        ]
    },
    "sensor.dallas_temp": {
        "config_entries": [
            {"key": "address", "type": "string"},
            {"key": "update_interval", "type": "string"},
        ]
    },
    "switch.gpio": {
        "config_entries": [
            {"key": "pin", "type": "pin"},
            {"key": "inverted", "type": "boolean"},
        ]
    },
    "switch.template": {
        "config_entries": [
            {"key": "lambda", "type": "string"},
            {"key": "optimistic", "type": "boolean"},
        ]
    },
    "binary_sensor.template": {
        "config_entries": [
            {"key": "lambda", "type": "string"},
        ]
    },
    "light.binary": {
        "config_entries": [
            {"key": "output", "type": "id"},
            {"key": "restore_mode", "type": "string"},
        ]
    },
}


def test_extract_emits_explicit_id_for_every_entry() -> None:
    """Every featured entry gets ``fields.id`` regardless of platform domain."""
    inline = {
        "binary_sensor": [{"platform": "gpio", "pin": 4}],
        "output": [{"platform": "gpio", "pin": 5}],
        "sensor": [{"platform": "dht", "pin": 14, "model": "DHT22"}],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)

    by_local = {entry["id"]: entry for entry in featured}
    assert by_local["binary_sensor_gpio_1"]["fields"]["id"] == "binary_sensor_gpio_1"
    assert by_local["output_gpio_1"]["fields"]["id"] == "output_gpio_1"
    assert by_local["sensor_dht_1"]["fields"]["id"] == "sensor_dht_1"


def test_extract_uses_upstream_name_for_entities() -> None:
    """Upstream's ``name:`` rides through verbatim for HA entity platforms."""
    inline = {
        "binary_sensor": [{"platform": "gpio", "name": "Front Door", "pin": 4}],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    assert featured[0]["fields"]["name"] == "Front Door"


def test_extract_derives_name_default_when_upstream_omits() -> None:
    """Entity platforms without an upstream ``name:`` fall back to a derived default."""
    inline = {
        "sensor": [{"platform": "dht", "pin": 14, "model": "DHT22"}],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    # ``<Platform> <counter>`` — keeps the entity surfaced in HA without
    # the user having to fill in a name first.
    assert featured[0]["fields"]["name"] == "Dht 1"


def test_extract_skips_name_for_non_entity_platforms() -> None:
    """Non-entity platforms (``output:``) get only ``id``, never ``name``."""
    inline = {
        "output": [{"platform": "gpio", "name": "ignored upstream", "pin": 5}],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    fields = featured[0]["fields"]
    assert "id" in fields
    assert "name" not in fields


def test_extract_counter_distinguishes_multiple_instances() -> None:
    """Two binary_sensor.gpio entries on the same page get distinct ids + name suffixes."""
    inline = {
        "binary_sensor": [
            {"platform": "gpio", "pin": 4},
            {"platform": "gpio", "pin": 5},
        ],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    ids = [f["fields"]["id"] for f in featured]
    names = [f["fields"]["name"] for f in featured]
    assert ids == ["binary_sensor_gpio_1", "binary_sensor_gpio_2"]
    assert names == ["Gpio 1", "Gpio 2"]


def test_extract_strips_template_substitution_from_name() -> None:
    """``${friendly_name} Relay1`` upstream becomes a clean ``Relay1`` preset name."""
    inline = {
        "switch": [{"platform": "gpio", "name": "${friendly_name} Relay1", "pin": 12}],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    assert featured[0]["fields"]["name"] == "Relay1"


def test_extract_occupancy_label_strips_template_and_drops_component_prefix() -> None:
    """``occupied_by`` exposes only the cleaned name, not ``switch.gpio (...)``."""
    inline = {
        "switch": [{"platform": "gpio", "name": "${friendly_name} Relay1", "pin": 12}],
    }
    _, _, gpio_occupancy = _extract_featured_components(inline, _INDEX)
    assert gpio_occupancy == {12: "Relay1"}


def test_extract_drops_component_with_placeholder_field_value() -> None:
    """``address: (FILL IN ONE-WIRE BUS ADDRESS)`` drops the whole sensor.dallas_temp."""
    inline = {
        "sensor": [
            {
                "platform": "dallas_temp",
                "address": "(FILL IN ONE-WIRE BUS ADDRESS)",
                "update_interval": "30s",
            },
        ],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    assert featured == []


def test_extract_skips_template_platform_entirely() -> None:
    """``switch.template`` etc. need user-supplied lambdas — never lifted as presets."""
    inline = {
        "switch": [{"platform": "template", "name": "Demo", "optimistic": True}],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    assert featured == []


def test_extract_skips_item_with_lambda_top_level_key() -> None:
    """Any inline item with a top-level ``lambda:`` is dropped — its behaviour is in the lambda."""
    inline = {
        "binary_sensor": [
            {
                "platform": "template",
                "name": "API Connected",
                "lambda": "return global_api_server->is_connected();",
            },
        ],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    assert featured == []


def test_extract_drops_id_reference_to_skipped_target() -> None:
    """Refs pointing at a non-kept component are silently omitted."""
    inline = {
        "light": [
            {"platform": "binary", "id": "indicator", "name": "Indicator", "output": "missing"},
        ],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    # Without other hardware-specific fields the consumer drops out
    # entirely — the placeholder ref didn't match any kept sibling so
    # the entry has no preset value to lock in.
    assert featured == []


def test_extract_skips_placeholder_component_without_polluting_pin_block() -> None:
    """Skipped placeholder components don't leave their GPIO in ``occupied_by``."""
    inline = {
        "sensor": [
            {
                "platform": "dallas_temp",
                "address": "(FILL IN)",
                "update_interval": "30s",
            },
        ],
        "switch": [{"platform": "gpio", "name": "Relay", "pin": 12}],
    }
    _, _, gpio_occupancy = _extract_featured_components(inline, _INDEX)
    # Only the surviving switch.gpio's pin lands in the occupancy map.
    assert gpio_occupancy == {12: "Relay"}


def test_extract_preserves_upstream_id_as_local_id() -> None:
    """Sanitized upstream ``id:`` becomes the manifest's local id when valid + free."""
    inline = {
        "output": [{"platform": "gpio", "id": "red_output", "pin": 4}],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    assert featured[0]["id"] == "red_output"
    assert featured[0]["fields"]["id"] == "red_output"


def test_extract_falls_back_when_upstream_id_invalid() -> None:
    """Upstream ids that can't be sanitized to a valid identifier fall back to default."""
    inline = {
        "output": [{"platform": "gpio", "id": "123-not-an-id", "pin": 4}],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    assert featured[0]["id"] == "output_gpio_1"


def test_extract_falls_back_on_local_id_collision() -> None:
    """Two siblings sharing an upstream id don't collide — second one falls back."""
    inline = {
        "output": [
            {"platform": "gpio", "id": "shared", "pin": 4},
            {"platform": "gpio", "id": "shared", "pin": 5},
        ],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    ids = [f["id"] for f in featured]
    assert ids == ["shared", "output_gpio_2"]


def test_extract_rewrites_id_reference_to_kept_sibling() -> None:
    """``light.binary.output: red_output`` resolves to the kept output's local id."""
    inline = {
        "output": [{"platform": "gpio", "id": "red_output", "pin": 4}],
        "light": [
            {"platform": "binary", "id": "indicator", "name": "Indicator", "output": "red_output"},
        ],
    }
    featured, _, _ = _extract_featured_components(inline, _INDEX)
    light = next(f for f in featured if f["component_id"] == "light.binary")
    assert light["fields"]["output"] == "red_output"


def test_extract_generates_bundle_for_id_referenced_components() -> None:
    """A consumer with id-ref dependencies emits a bundle adding deps then the consumer."""
    inline = {
        "output": [{"platform": "gpio", "id": "red_output", "pin": 4}],
        "light": [
            {"platform": "binary", "id": "indicator", "name": "Indicator", "output": "red_output"},
        ],
    }
    _, bundles, _ = _extract_featured_components(inline, _INDEX)
    assert len(bundles) == 1
    bundle = bundles[0]
    # Dependencies first so the consumer's ``output:`` ref already
    # resolves when the dashboard adds it.
    assert bundle["component_ids"] == ["red_output", "indicator"]
    assert bundle["name"] == "Indicator (full setup)"
    assert bundle["id"] == "indicator_setup"


def test_extract_skips_bundle_when_no_dependencies_resolve() -> None:
    """Standalone components (no id refs) don't get a bundle."""
    inline = {
        "switch": [{"platform": "gpio", "name": "Relay", "pin": 12}],
    }
    _, bundles, _ = _extract_featured_components(inline, _INDEX)
    assert bundles == []


# Index entries for the I/O-expander extraction. The hub carries an
# ``address`` and an ``id``; i2c carries pin-type ``sda`` / ``scl`` so the
# bus pins land in occupancy. ``pcf8574`` depends on ``i2c`` so the bus is
# pulled in as a prerequisite.
_EXPANDER_INDEX = {
    "binary_sensor.gpio": {"config_entries": [{"key": "pin", "type": "pin"}]},
    "pcf8574": {
        "dependencies": ["i2c"],
        "config_entries": [
            {"key": "id", "type": "id"},
            {"key": "address", "type": "string"},
        ],
    },
    "i2c": {
        "config_entries": [
            {"key": "id", "type": "id"},
            {"key": "sda", "type": "pin"},
            {"key": "scl", "type": "pin"},
        ],
    },
}


def _expander_config(*, i2c: list | None = None, pcf8574: list | None = None) -> dict:
    """Build an expander board config; override ``i2c`` / ``pcf8574`` for an edge case."""
    return {
        "i2c": i2c if i2c is not None else [{"id": "bus_a", "sda": 9, "scl": 10}],
        "pcf8574": pcf8574
        if pcf8574 is not None
        else [{"id": "pcf8574_hub_in_1", "address": 0x21}],
        "binary_sensor": [
            {
                "platform": "gpio",
                "name": "Input 1",
                "pin": {"pcf8574": "pcf8574_hub_in_1", "number": 0, "mode": "INPUT"},
            }
        ],
    }


def test_extract_expander_hubs_materializes_hub_and_bus_with_locked_ids() -> None:
    """A gpio pin on a pcf8574 lifts the hub + i2c bus, ids locked to the source."""
    config = _expander_config()
    featured, _, _ = _extract_featured_components(config, _EXPANDER_INDEX)
    extra, occupancy = _extract_expander_hubs(config, featured, _EXPANDER_INDEX)

    by_id = {e["id"]: e for e in extra}
    # The hub's id is locked so it matches the pin's baked reference.
    assert by_id["pcf8574_hub_in_1"]["component_id"] == "pcf8574"
    assert by_id["pcf8574_hub_in_1"]["fields"]["id"] == {
        "value": "pcf8574_hub_in_1",
        "locked": True,
    }
    assert by_id["pcf8574_hub_in_1"]["requires"] == ["bus_a"]
    assert by_id["bus_a"]["component_id"] == "i2c"
    assert by_id["bus_a"]["fields"]["id"] == {"value": "bus_a", "locked": True}
    # The i2c pins occupy real board GPIOs; the expander channel does not.
    assert occupancy == {9: "bus_a", 10: "bus_a"}


def test_extract_expander_hubs_wires_consumer_requires() -> None:
    """The gpio consumer requires its bus then its hub, in order."""
    config = _expander_config()
    featured, _, _ = _extract_featured_components(config, _EXPANDER_INDEX)
    _extract_expander_hubs(config, featured, _EXPANDER_INDEX)
    consumer = next(e for e in featured if e["component_id"] == "binary_sensor.gpio")
    assert consumer["requires"] == ["bus_a", "pcf8574_hub_in_1"]


def test_extract_expander_pin_does_not_occupy_a_board_gpio() -> None:
    """The expander channel ``number`` is never recorded as a board GPIO."""
    config = _expander_config()
    _, _, occupancy = _extract_featured_components(config, _EXPANDER_INDEX)
    # Channel 0 is an expander channel, not board GPIO 0.
    assert 0 not in occupancy


def test_extract_expander_hub_with_synthetic_id() -> None:
    """A sole hub block with no ``id`` adopts the pin's referenced id."""
    # No ``id`` on the single hub — ESPHome would auto-generate one.
    config = _expander_config(pcf8574=[{"address": 0x21}])
    featured, _, _ = _extract_featured_components(config, _EXPANDER_INDEX)
    extra, _ = _extract_expander_hubs(config, featured, _EXPANDER_INDEX)
    by_id = {e["id"]: e for e in extra}
    assert by_id["pcf8574_hub_in_1"]["fields"]["id"] == {
        "value": "pcf8574_hub_in_1",
        "locked": True,
    }
    assert by_id["pcf8574_hub_in_1"]["fields"]["address"]  # hardware lifted from the sole block
    consumer = next(e for e in featured if e["component_id"] == "binary_sensor.gpio")
    assert "pcf8574_hub_in_1" in (consumer.get("requires") or [])


def test_extract_expander_hub_with_placeholder_field_is_skipped() -> None:
    """A placeholder in the hub block skips the hub (and its bus) entirely — no orphan."""
    config = _expander_config(pcf8574=[{"id": "pcf8574_hub_in_1", "address": "(FILL IN ADDRESS)"}])
    featured, _, _ = _extract_featured_components(config, _EXPANDER_INDEX)
    extra, occupancy = _extract_expander_hubs(config, featured, _EXPANDER_INDEX)
    # Neither the hub nor the bus it would have used is materialized, and the
    # bus's pins don't leak into occupancy (the skip happens before the bus).
    assert extra == []
    assert occupancy == {}
    # The consumer is dropped too — keeping it would ship a dangling pin ref.
    assert not any(e["component_id"] == "binary_sensor.gpio" for e in featured)


# A shift-register hub (unlike an i2c expander) drives board GPIOs directly, so
# its own block carries real board pins — the occupancy-leak surface.
_SHIFT_REGISTER_INDEX = {
    "switch.gpio": {"config_entries": [{"key": "pin", "type": "pin"}]},
    "sn74hc595": {
        "config_entries": [
            {"key": "id", "type": "id"},
            {"key": "data_pin", "type": "pin"},
            {"key": "clock_pin", "type": "pin"},
            {"key": "latch_pin", "type": "pin"},
        ],
    },
}


def _shift_register_config(latch_pin: object) -> dict:
    return {
        "sn74hc595": [{"id": "sr1", "data_pin": 21, "clock_pin": 22, "latch_pin": latch_pin}],
        "switch": [
            {
                "platform": "gpio",
                "name": "Relay 1",
                "pin": {"sn74hc595": "sr1", "number": 0},
            }
        ],
    }


def test_extract_shift_register_hub_placeholder_does_not_leak_board_pins() -> None:
    """A placeholder part-way through a shift-register block leaves no board pin occupied."""
    config = _shift_register_config("(FILL IN LATCH PIN)")
    featured, _, _ = _extract_featured_components(config, _SHIFT_REGISTER_INDEX)
    extra, occupancy = _extract_expander_hubs(config, featured, _SHIFT_REGISTER_INDEX)
    # data_pin/clock_pin are recorded before the placeholder latch_pin aborts the
    # hub; dropping the hub must drop those pins too, not strand them occupied.
    assert extra == []
    assert occupancy == {}


def test_extract_shift_register_hub_records_its_board_pins() -> None:
    """A complete shift-register hub occupies the board GPIOs it drives."""
    config = _shift_register_config(23)
    featured, _, _ = _extract_featured_components(config, _SHIFT_REGISTER_INDEX)
    extra, occupancy = _extract_expander_hubs(config, featured, _SHIFT_REGISTER_INDEX)
    assert {e["id"] for e in extra} == {"sr1"}
    assert set(occupancy) == {21, 22, 23}


def test_extract_expander_ambiguous_multi_hub_is_skipped() -> None:
    """Two hub blocks, neither matching the pin's id, can't be disambiguated → no guess."""
    config = _expander_config(
        pcf8574=[{"id": "hub_x", "address": 0x21}, {"id": "hub_y", "address": 0x22}]
    )
    featured, _, _ = _extract_featured_components(config, _EXPANDER_INDEX)
    extra, _ = _extract_expander_hubs(config, featured, _EXPANDER_INDEX)
    assert extra == []
    # An unresolvable hub drops its consumer rather than leaving a dangling ref.
    assert not any(e["component_id"] == "binary_sensor.gpio" for e in featured)


def test_extract_expander_bus_without_id_not_materialized() -> None:
    """An i2c bus with no upstream id isn't locked; the hub still lands, requiring only itself."""
    config = _expander_config(i2c=[{"sda": 9, "scl": 10}])  # no id to lock onto
    featured, _, _ = _extract_featured_components(config, _EXPANDER_INDEX)
    extra, _ = _extract_expander_hubs(config, featured, _EXPANDER_INDEX)
    assert not any(e["component_id"] == "i2c" for e in extra)
    assert any(e["component_id"] == "pcf8574" for e in extra)
    consumer = next(e for e in featured if e["component_id"] == "binary_sensor.gpio")
    assert consumer["requires"] == ["pcf8574_hub_in_1"]


def test_extract_expander_picks_the_bus_the_hub_pins_via_i2c_id() -> None:
    """With two i2c buses, the hub's ``i2c_id`` selects which one is lifted (and locked on)."""
    config = _expander_config(
        i2c=[{"id": "bus_a", "sda": 5, "scl": 16}, {"id": "bus_b", "sda": 15, "scl": 4}],
        pcf8574=[{"id": "pcf8574_hub_in_1", "i2c_id": "bus_b", "address": 0x24}],
    )
    featured, _, _ = _extract_featured_components(config, _EXPANDER_INDEX)
    extra, occupancy = _extract_expander_hubs(config, featured, _EXPANDER_INDEX)
    by_id = {e["id"]: e for e in extra}
    # Only the referenced bus is lifted, with its own pins.
    assert "bus_b" in by_id
    assert "bus_a" not in by_id
    assert by_id["bus_b"]["fields"]["sda"] == {"value": 15, "locked": True}
    # The hub keeps its i2c_id so it binds to that bus, not esphome's defaults.
    assert by_id["pcf8574_hub_in_1"]["fields"]["i2c_id"] == {"value": "bus_b", "locked": True}
    assert by_id["pcf8574_hub_in_1"]["requires"] == ["bus_b"]
    consumer = next(e for e in featured if e["component_id"] == "binary_sensor.gpio")
    assert consumer["requires"] == ["bus_b", "pcf8574_hub_in_1"]
    assert occupancy == {15: "bus_b", 4: "bus_b"}
