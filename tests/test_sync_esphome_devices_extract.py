"""
Tests for ``_extract_featured_components`` in ``script/sync_esphome_devices.py``.

Focuses on the explicit-fields contract: every emitted featured-component
entry must carry ``fields.id`` and, for HA entity domains, ``fields.name``
— so the imported manifests are self-contained and the runtime never
has to auto-derive these from the local id / display name.
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
}


def test_extract_emits_explicit_id_for_every_entry() -> None:
    """Every featured entry gets ``fields.id`` regardless of platform domain."""
    inline = {
        "binary_sensor": [{"platform": "gpio", "pin": 4}],
        "output": [{"platform": "gpio", "pin": 5}],
        "sensor": [{"platform": "dht", "pin": 14, "model": "DHT22"}],
    }
    featured, _ = _extract_featured_components(inline, _INDEX)

    by_local = {entry["id"]: entry for entry in featured}
    assert by_local["binary_sensor_gpio_1"]["fields"]["id"] == "binary_sensor_gpio_1"
    assert by_local["output_gpio_1"]["fields"]["id"] == "output_gpio_1"
    assert by_local["sensor_dht_1"]["fields"]["id"] == "sensor_dht_1"


def test_extract_uses_upstream_name_for_entities() -> None:
    """Upstream's ``name:`` rides through verbatim for HA entity platforms."""
    inline = {
        "binary_sensor": [{"platform": "gpio", "name": "Front Door", "pin": 4}],
    }
    featured, _ = _extract_featured_components(inline, _INDEX)
    assert featured[0]["fields"]["name"] == "Front Door"


def test_extract_derives_name_default_when_upstream_omits() -> None:
    """Entity platforms without an upstream ``name:`` fall back to a derived default."""
    inline = {
        "sensor": [{"platform": "dht", "pin": 14, "model": "DHT22"}],
    }
    featured, _ = _extract_featured_components(inline, _INDEX)
    # ``<Platform> <counter>`` — keeps the entity surfaced in HA without
    # the user having to fill in a name first.
    assert featured[0]["fields"]["name"] == "Dht 1"


def test_extract_skips_name_for_non_entity_platforms() -> None:
    """Non-entity platforms (``output:``) get only ``id``, never ``name``."""
    inline = {
        "output": [{"platform": "gpio", "name": "ignored upstream", "pin": 5}],
    }
    featured, _ = _extract_featured_components(inline, _INDEX)
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
    featured, _ = _extract_featured_components(inline, _INDEX)
    ids = [f["fields"]["id"] for f in featured]
    names = [f["fields"]["name"] for f in featured]
    assert ids == ["binary_sensor_gpio_1", "binary_sensor_gpio_2"]
    assert names == ["Gpio 1", "Gpio 2"]
