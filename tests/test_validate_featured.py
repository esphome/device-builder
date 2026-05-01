"""Tests for the featured-components cross-catalog validation in ``script/validate_definitions.py``.

These poke the validator's pure helpers directly with synthetic manifest
fragments rather than spinning up real boards on disk — that way each
case stays isolated and we don't have to ship deliberately-broken
manifests in ``definitions/boards/``.
"""

from __future__ import annotations

from script.validate_definitions import (  # type: ignore[import-not-found]
    _build_components_index,
    _validate_featured,
)


def _index() -> dict | None:
    return _build_components_index()


def _board(featured: list[dict] | None = None, bundles: list[dict] | None = None) -> dict:
    return {
        "featured_components": featured or [],
        "featured_bundles": bundles or [],
    }


def _pins(*gpios: int) -> dict[int, dict]:
    """Build a pins_by_gpio map with every pin marked as having no features."""
    return {g: {"gpio": g, "features": []} for g in gpios}


def test_valid_locked_pin() -> None:
    """A featured switch.gpio with a locked, declared pin passes."""
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "relay",
                    "component_id": "switch.gpio",
                    "fields": {"pin": {"value": 12, "locked": True}},
                }
            ]
        ),
        _pins(12),
        _index(),
    )
    assert errors == []


def test_unknown_component_id() -> None:
    errors = _validate_featured(
        "demo",
        _board([{"id": "foo", "component_id": "definitely.not.real", "fields": {}}]),
        {},
        _index(),
    )
    assert any("not found in components.json" in e for e in errors)


def test_unknown_field_key() -> None:
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "relay",
                    "component_id": "switch.gpio",
                    "fields": {"not_a_real_field": 1},
                }
            ]
        ),
        _pins(12),
        _index(),
    )
    assert any("not a config_entry on switch.gpio" in e for e in errors)


def test_pin_not_declared() -> None:
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "relay",
                    "component_id": "switch.gpio",
                    "fields": {"pin": {"value": 99, "locked": True}},
                }
            ]
        ),
        _pins(12),  # GPIO99 is not a declared pin
        _index(),
    )
    assert any("GPIO 99 not declared in pins" in e for e in errors)


def test_suggestions_pin_not_declared() -> None:
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "pir",
                    "component_id": "binary_sensor.gpio",
                    "fields": {"pin": {"suggestions": [4, 99]}},
                }
            ]
        ),
        _pins(4),
        _index(),
    )
    assert any("GPIO 99 not declared in pins" in e for e in errors)


def test_dict_pin_with_number_validates() -> None:
    """Rich pin form ({number, mode, inverted}) gets its GPIO checked."""
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "button",
                    "component_id": "binary_sensor.gpio",
                    "fields": {
                        "pin": {
                            "value": {
                                "number": 0,
                                "mode": {"input": True, "pullup": True},
                                "inverted": True,
                            },
                            "locked": True,
                        }
                    },
                }
            ]
        ),
        _pins(0),
        _index(),
    )
    assert errors == []


def test_duplicate_featured_id() -> None:
    errors = _validate_featured(
        "demo",
        _board(
            [
                {"id": "relay", "component_id": "switch.gpio", "fields": {}},
                {"id": "relay", "component_id": "switch.gpio", "fields": {}},
            ]
        ),
        {},
        _index(),
    )
    assert any("duplicate id 'relay'" in e for e in errors)


def test_duplicate_bundle_id() -> None:
    errors = _validate_featured(
        "demo",
        _board(
            [{"id": "a", "component_id": "switch.gpio", "fields": {}}],
            [
                {"id": "led", "name": "LED", "component_ids": ["a"]},
                {"id": "led", "name": "LED2", "component_ids": ["a"]},
            ],
        ),
        {},
        _index(),
    )
    assert any("duplicate id 'led'" in e for e in errors)


def test_bundle_unknown_component_id() -> None:
    errors = _validate_featured(
        "demo",
        _board(
            [{"id": "a", "component_id": "switch.gpio", "fields": {}}],
            [
                {"id": "b", "name": "Bundle", "component_ids": ["a", "ghost"]},
            ],
        ),
        {},
        _index(),
    )
    assert any("'ghost' does not match any" in e for e in errors)


def test_locked_and_suggestions_both_set() -> None:
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "x",
                    "component_id": "binary_sensor.gpio",
                    "fields": {"pin": {"locked": True, "suggestions": [4, 5]}},
                }
            ]
        ),
        _pins(4, 5),
        _index(),
    )
    assert any("cannot set both 'locked' and 'suggestions'" in e for e in errors)
