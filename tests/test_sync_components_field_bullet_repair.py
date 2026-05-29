"""Contract tests for ``_repair_field_bullet_descriptions``."""

from __future__ import annotations

from script.sync_components import (  # type: ignore[import-not-found]
    _repair_field_bullet_descriptions,
)

_UMBRELLA_PROSE = (
    "The `debug` component can be used to debug problems with ESPHome. "
    "At startup, it prints a bunch of useful information like reset "
    "reason, free heap size, ESPHome version and so on."
)


def test_replaces_optional_bullet_with_umbrella_description() -> None:
    entries = [
        {"id": "debug", "description": _UMBRELLA_PROSE},
        {
            "id": "sensor.debug",
            "description": (
                "- **free** (*Optional*): Reports the free heap size in bytes. "
                "All options from [Sensor](https://esphome.io/components/sensor)."
            ),
        },
    ]
    _repair_field_bullet_descriptions(entries)
    assert entries[1]["description"] == _UMBRELLA_PROSE


def test_replaces_required_bullet_with_umbrella_description() -> None:
    entries = [
        {"id": "foo", "description": _UMBRELLA_PROSE},
        {
            "id": "sensor.foo",
            "description": "- **bar** (*Required*): something or other.",
        },
    ]
    _repair_field_bullet_descriptions(entries)
    assert entries[1]["description"] == _UMBRELLA_PROSE


def test_clears_bullet_when_no_umbrella_entry_exists() -> None:
    entries = [
        {
            "id": "sensor.orphan",
            "description": "- **baz** (*Optional*): no parent in this catalog.",
        },
    ]
    _repair_field_bullet_descriptions(entries)
    assert entries[0]["description"] == ""


def test_clears_bullet_when_umbrella_description_is_empty() -> None:
    entries = [
        {"id": "ghost", "description": ""},
        {
            "id": "sensor.ghost",
            "description": "- **x** (*Optional*): unused.",
        },
    ]
    _repair_field_bullet_descriptions(entries)
    assert entries[1]["description"] == ""


def test_leaves_normal_descriptions_untouched() -> None:
    """The pass must not touch entries that already have a real description."""
    entries = [
        {"id": "wifi", "description": "Connects the ESP to WiFi."},
        {"id": "sensor.dht", "description": "DHT temperature/humidity sensor."},
        {"id": "switch.gpio", "description": ""},
    ]
    _repair_field_bullet_descriptions(entries)
    assert entries[0]["description"] == "Connects the ESP to WiFi."
    assert entries[1]["description"] == "DHT temperature/humidity sensor."
    assert entries[2]["description"] == ""


def test_does_not_match_field_bullets_inside_prose() -> None:
    """A leading ``- **foo**`` is required at the start; mid-string matches don't count."""
    desc = (
        "DHT temperature/humidity sensor. Fields include - **temperature** "
        "(*Optional*) and humidity sub-readings."
    )
    entries = [
        {"id": "dht", "description": _UMBRELLA_PROSE},
        {"id": "sensor.dht", "description": desc},
    ]
    _repair_field_bullet_descriptions(entries)
    assert entries[1]["description"] == desc


def test_handles_bare_stem_components_with_bullet_description() -> None:
    """A bullet-shaped description on a bare stem (no dot) gets cleared."""
    entries = [
        {"id": "rare_bug", "description": "- **field** (*Optional*): bad scrape."},
    ]
    _repair_field_bullet_descriptions(entries)
    assert entries[0]["description"] == ""


def test_skips_stem_lookup_for_synthetic_umbrella_domains() -> None:
    """Skip stem-based umbrella for ``ota``/``time`` domains -- stem can collide."""
    entries = [
        # The core ``esphome`` component is a real catalog entry with its
        # own description, but it has no relationship to ``ota.esphome``.
        # Substituting would land the core config description on an OTA
        # platform entry.
        {"id": "esphome", "description": "Core ESPHome configuration."},
        {
            "id": "ota.esphome",
            "description": "- **password** (*Optional*): the OTA password.",
        },
    ]
    _repair_field_bullet_descriptions(entries)
    assert entries[0]["description"] == "Core ESPHome configuration."
    assert entries[1]["description"] == ""
