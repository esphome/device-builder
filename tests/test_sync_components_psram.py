"""
Tests for psram's synthesized structured editor.

psram's ``CONFIG_SCHEMA`` is a per-variant callable the schema bundle dumps
empty; ``_psram_config_entries`` recovers the fields by capturing the built
``vol.Schema`` for every ESP32 variant and unioning the keys / options /
defaults. Pins that union so the editor can't silently regress to YAML-only.
"""

from __future__ import annotations

from script.sync_components import _psram_config_entries  # type: ignore[import-not-found]


def _by_key() -> dict[str, dict]:
    return {entry["key"]: entry for entry in _psram_config_entries()}


def test_entries_cover_every_field_but_the_auto_id() -> None:
    """The five user-facing fields are surfaced; the GenerateID is dropped."""
    assert set(_by_key()) == {
        "mode",
        "speed",
        "enable_ecc",
        "disabled",
        "ignore_not_found",
    }


def test_mode_is_a_select_of_every_variant_mode() -> None:
    """``mode`` unions quad/octal/hex, tagging each with the variants that accept it."""
    mode = _by_key()["mode"]
    assert mode["type"] == "string"
    by_value = {o["value"]: o["variants"] for o in mode["options"]}
    assert set(by_value) == {"quad", "octal", "hex"}
    # Variants are lowercased to match the board catalog ``esphome.variant`` form.
    assert by_value["octal"] == ["esp32s3"]
    assert by_value["hex"] == ["esp32p4"]
    assert "esp32" in by_value["quad"] and "esp32s3" in by_value["quad"]
    # No single default is valid on every chip (P4 needs hex, not quad).
    assert "default_value" not in mode
    assert not mode.get("advanced")


def test_speed_unions_all_variant_speeds_ascending() -> None:
    """``speed`` covers the P4 (20/100/200) and classic (40/80/120) sets, variant-tagged."""
    speed = _by_key()["speed"]
    by_value = {o["value"]: o["variants"] for o in speed["options"]}
    values = list(by_value)
    assert {"40MHZ", "80MHZ", "120MHZ", "200MHZ"} <= set(values)
    assert values == sorted(values, key=lambda v: int(v[:-3]))
    # 20MHZ is P4-only; 40MHZ covers the classic chips.
    assert by_value["20MHZ"] == ["esp32p4"]
    assert "esp32" in by_value["40MHZ"]
    # P4's 40MHZ is invalid, so no cross-chip default is shipped.
    assert "default_value" not in speed
    assert not speed.get("advanced")


def test_booleans_keep_their_upstream_defaults_behind_advanced() -> None:
    """The three flags are booleans with ignore_not_found defaulting True."""
    entries = _by_key()
    for key, default in (
        ("enable_ecc", False),
        ("disabled", False),
        ("ignore_not_found", True),
    ):
        entry = entries[key]
        assert entry["type"] == "boolean"
        assert entry["default_value"] is default
        assert entry["advanced"] is True
