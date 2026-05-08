"""Tests for the long-form pin schema attached by ``_pin_long_form_extras``.

ESPHome's pin schema accepts both ``pin: GPIO5`` (the short form
the existing pin picker handles) and the long form
``pin: { number: GPIO5, mode: { input: true, pullup: true },
inverted: true }``. Without nested config_entries on every
``type: pin`` catalog entry the visual editor can't drive the
long form — issue #420.

These tests pin:

- ``_pin_long_form_extras`` extracts ``mode`` (with the standard
  five flag children) and ``inverted`` from ``esp32.json``'s
  pin schema.
- ``_convert_field`` attaches the extras as nested
  ``config_entries`` on every ``type: pin`` entry it produces.
- The bundle-missing fallback returns ``()`` so a sync against a
  truncated bundle doesn't crash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _convert_field,
    _pin_long_form_extras,
    _pin_long_form_has_inverted,
    _pin_long_form_mode_flags,
)

# Minimal esp32.json shape covering the fields ``_pin_long_form_extras``
# reads. Mirrors the actual bundle structure — top-level ``esp32`` key,
# ``pin.schema.config_vars``, with ``mode`` carrying its own nested
# ``schema.config_vars`` of mode-flag booleans. Keep this in sync with
# the bundle if upstream restructures the pin definition.
_ESP32_PIN_BUNDLE: dict = {
    "esp32": {
        "pin": {
            "schema": {
                "config_vars": {
                    "number": {"key": "Required"},
                    "mode": {
                        "default": "{}",
                        "key": "Optional",
                        "modes": [],
                        "schema": {
                            "config_vars": {
                                "input": {"default": "False", "key": "Optional", "type": "boolean"},
                                "output": {
                                    "default": "False",
                                    "key": "Optional",
                                    "type": "boolean",
                                },
                                "pullup": {
                                    "default": "False",
                                    "key": "Optional",
                                    "type": "boolean",
                                },
                                "pulldown": {
                                    "default": "False",
                                    "key": "Optional",
                                    "type": "boolean",
                                },
                                "open_drain": {
                                    "default": "False",
                                    "key": "Optional",
                                    "type": "boolean",
                                },
                            },
                        },
                        "type": "pin",
                    },
                    "inverted": {
                        "default": "False",
                        "key": "Optional",
                        "type": "boolean",
                    },
                    "drive_strength": {
                        "default": "20mA",
                        "key": "Optional",
                        "type": "enum",
                    },
                },
            },
            "type": "schema",
        },
    },
}


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    """``schema_dir`` containing a stub ``esp32.json`` with the pin block.

    Clears the bundle-parse caches so each test sees this fixture's
    bundle rather than a previous test's. ``_pin_long_form_mode_flags``
    / ``_pin_long_form_has_inverted`` are ``@cache``-decorated for
    production efficiency (the pin block gets parsed once per sync
    run, even though hundreds of pin entries query it); tests that
    vary the bundle content have to invalidate them explicitly.
    """
    (tmp_path / "esp32.json").write_text(json.dumps(_ESP32_PIN_BUNDLE))
    _pin_long_form_mode_flags.cache_clear()
    _pin_long_form_has_inverted.cache_clear()
    return tmp_path


def test_pin_long_form_extras_extracts_mode_and_inverted(schema_dir: Path) -> None:
    """``mode`` (nested + 5 flag booleans) and ``inverted`` (boolean) emitted.

    Pins the canonical shape so a refactor that drops one of the
    flag children, flips the mode wrapper to a flat structure, or
    accidentally bubbles ``drive_strength`` (an ESP32-only extra
    that doesn't apply to other platforms' pin schemas) surfaces
    here.
    """
    extras = _pin_long_form_extras(schema_dir)

    assert [e["key"] for e in extras] == ["mode", "inverted"]

    mode = extras[0]
    assert mode["type"] == "nested"
    assert mode["advanced"] is True
    assert [c["key"] for c in mode["config_entries"]] == [
        "input",
        "output",
        "pullup",
        "pulldown",
        "open_drain",
    ]
    for child in mode["config_entries"]:
        assert child["type"] == "boolean"
        assert child["default_value"] is False
        assert child["advanced"] is True

    inverted = extras[1]
    assert inverted["type"] == "boolean"
    assert inverted["default_value"] is False
    assert inverted["advanced"] is True


def test_pin_long_form_extras_skips_platform_specific_drive_strength(
    schema_dir: Path,
) -> None:
    """ESP32's ``drive_strength`` does not leak into the synthesised extras.

    Catalog entries are component-keyed, not platform-keyed — a
    ``binary_sensor.gpio.pin`` config var renders the same on
    every platform. ESP32-specific fields like ``drive_strength``
    would render but do nothing on esp8266 / rp2040 / nrf52 /
    host configs, which is worse than not exposing them.
    """
    extras = _pin_long_form_extras(schema_dir)

    keys = {e["key"] for e in extras}
    assert "drive_strength" not in keys


def test_pin_long_form_extras_returns_empty_when_bundle_missing(
    tmp_path: Path,
) -> None:
    """No ``esp32.json`` in the bundle → empty tuple, not a crash.

    Defensive against a truncated / corrupt bundle. The pin
    entries fall back to the short-form picker (today's
    behaviour); the sync run completes normally.
    """
    _pin_long_form_mode_flags.cache_clear()
    _pin_long_form_has_inverted.cache_clear()
    extras = _pin_long_form_extras(tmp_path)
    assert extras == ()


@pytest.mark.parametrize(
    "malformed_payload",
    [
        # Top-level isn't a dict (a JSON-decodable value but the
        # wrong shape).
        "null",
        "[]",
        '"not-an-object"',
        "42",
        # Top-level dict but ``esp32`` key carries the wrong type.
        '{"esp32": null}',
        '{"esp32": []}',
        # ``esp32.pin`` non-dict.
        '{"esp32": {"pin": "string-value"}}',
        # ``esp32.pin.schema.config_vars.mode`` non-dict — the
        # path-walk should fail closed mid-traversal.
        '{"esp32": {"pin": {"schema": {"config_vars": {"mode": []}}}}}',
        # Outright invalid JSON.
        "{not json",
    ],
)
def test_pin_long_form_extras_returns_empty_for_malformed_bundle(
    tmp_path: Path,
    malformed_payload: str,
) -> None:
    """Bundle present but malformed → empty tuple, not an AttributeError.

    Defensive against a partial download, a future schema-shape
    change that breaks our path expectations, or a hand-edited
    bundle in a developer cache. The sync should keep running and
    fall back to short-form pin entries; an ``AttributeError`` on
    a chained ``.get()`` call against a non-dict node would crash
    the whole catalog regen.
    """
    (tmp_path / "esp32.json").write_text(malformed_payload)
    _pin_long_form_mode_flags.cache_clear()
    _pin_long_form_has_inverted.cache_clear()
    assert _pin_long_form_extras(tmp_path) == ()


def test_convert_field_attaches_long_form_extras_to_pin_entries(
    schema_dir: Path,
) -> None:
    """A ``type: pin`` config_var lands with the long-form extras nested.

    End-to-end check that ``_convert_field`` actually wires
    ``_pin_long_form_extras`` into the produced entry — without
    this the catalog stays short-form-only and the visual editor
    bug behind issue #420 persists.
    """
    raw = {"key": "Required", "modes": ["input"], "type": "pin"}
    entry = _convert_field("pin", raw, schema_dir)

    assert entry is not None
    assert entry["type"] == "pin"
    assert entry["config_entries"] is not None
    assert [e["key"] for e in entry["config_entries"]] == ["mode", "inverted"]


def test_convert_field_does_not_attach_pin_extras_to_non_pin_entries(
    schema_dir: Path,
) -> None:
    """Non-pin fields keep their existing ``config_entries`` shape (None / nested).

    The pin extras are scoped to ``type: pin`` only — a regression
    that flooded every entry with the long-form pin fields would
    break every other field's editor.
    """
    raw = {"key": "Required", "type": "string"}
    entry = _convert_field("name", raw, schema_dir)

    assert entry is not None
    assert entry["type"] == "string"
    assert entry["config_entries"] is None


def test_pin_long_form_extras_returns_fresh_dicts_per_call(
    schema_dir: Path,
) -> None:
    """Each call rebuilds the dicts so downstream mutation can't cross-leak.

    Sync passes after ``_convert_field`` modify ``config_entries``
    in place (description rewrites, sub-field defaults, etc.). If
    ``_pin_long_form_extras`` returned shared dict instances, a
    mutation on one component's pin entry would silently affect
    every other pin entry across the whole catalog.

    Mutating the first call's structures must not change the
    second call's identity / shape.
    """
    first = _pin_long_form_extras(schema_dir)
    second = _pin_long_form_extras(schema_dir)

    # Top-level dicts — fresh objects.
    assert first[0] is not second[0]
    assert first[1] is not second[1]
    # Nested mode-children list — fresh objects too (the leak risk
    # is highest here; sync passes commonly walk into nested
    # children).
    assert first[0]["config_entries"][0] is not second[0]["config_entries"][0]

    # Stress-test: mutate first call's structures, second call
    # must come back clean.
    first[0]["config_entries"].append({"injected": True})
    first[0]["description"] = "MUTATED"

    third = _pin_long_form_extras(schema_dir)
    assert all("injected" not in c for c in third[0]["config_entries"])
    assert third[0]["description"] != "MUTATED"
