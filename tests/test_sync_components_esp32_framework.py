"""
Tests for esp32's framework ``type`` default.

ESPHome sets the framework default at validate time, so the schema bundle dumps
``framework.type`` with options but no default; ``_apply_esp32_options`` recovers
esp-idf from introspection. Pins that the applier sets it only for esp32 and
only when esp-idf is a real option.
"""

from __future__ import annotations

from script.sync_components import _apply_esp32_options  # type: ignore[import-not-found]


def _framework_entries() -> list[dict]:
    return [
        {
            "key": "framework",
            "type": "nested",
            "config_entries": [
                {
                    "key": "type",
                    "type": "string",
                    "options": [{"value": "esp-idf"}, {"value": "arduino"}],
                },
                {"key": "version", "type": "string", "default_value": "recommended"},
            ],
        },
    ]


def _type_entry(entries: list[dict]) -> dict:
    framework = next(e for e in entries if e["key"] == "framework")
    return next(e for e in framework["config_entries"] if e["key"] == "type")


def test_framework_type_defaults_to_esp_idf() -> None:
    """The nested ``framework.type`` gets esp-idf, one of its own options."""
    entries = _framework_entries()
    _apply_esp32_options("esp32", entries)
    type_entry = _type_entry(entries)
    assert type_entry["default_value"] == "esp-idf"
    assert "esp-idf" in {o["value"] for o in type_entry["options"]}


def test_non_esp32_component_is_untouched() -> None:
    """A non-esp32 id leaves the framework entry alone."""
    entries = _framework_entries()
    _apply_esp32_options("logger", entries)
    assert "default_value" not in _type_entry(entries)


def test_missing_esp_idf_option_sets_no_default() -> None:
    """The default is never set when esp-idf isn't an offered option."""
    entries = _framework_entries()
    _type_entry(entries)["options"] = [{"value": "arduino"}]
    _apply_esp32_options("esp32", entries)
    assert "default_value" not in _type_entry(entries)


def test_existing_default_is_not_clobbered() -> None:
    """A default the bundle already carries wins over the applier's."""
    entries = _framework_entries()
    _type_entry(entries)["default_value"] = "arduino"
    _apply_esp32_options("esp32", entries)
    assert _type_entry(entries)["default_value"] == "arduino"
