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
