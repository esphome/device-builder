"""Tests for the featured-components cross-catalog validation in ``script/validate_definitions.py``.

These poke the validator's pure helpers directly with synthetic manifest
fragments rather than spinning up real boards on disk — that way each
case stays isolated and we don't have to ship deliberately-broken
manifests in ``definitions/boards/``.
"""

from __future__ import annotations

import pytest

from script.validate_definitions import (  # type: ignore[import-not-found]
    _build_components_index,
    _validate_featured,
    _validate_featured_dependencies,
    _validate_field_preset,
)


def _leaf(component_id: str, fid: str) -> dict:
    """Return a minimal finalized featured-component entry."""
    return {"id": fid, "component_id": component_id, "fields": {"id": {"value": fid}}}


# Co-locate with the rest of the catalog-heavy suite so we don't burn a
# second xdist worker re-reading ``components.index.json``.
pytestmark = pytest.mark.xdist_group("catalog")


@pytest.fixture(scope="module")
def _index() -> dict | None:
    """Parse ``components.index.json`` once per module — every test wants the same index.

    ``_build_components_index`` re-reads and JSON-decodes the index on
    each call; sharing one snapshot across the file's tests turns
    ten loads into one without changing the validator's contract.
    """
    return _build_components_index()


def _board(
    featured: list[dict] | None = None,
    bundles: list[dict] | None = None,
    defaults: list[str] | None = None,
) -> dict:
    return {
        "featured_components": featured or [],
        "featured_bundles": bundles or [],
        "default_components": defaults or [],
    }


def _pins(*gpios: int) -> dict[int, dict]:
    """Build a pins_by_gpio map with every pin marked as having no features."""
    return {g: {"gpio": g, "features": []} for g in gpios}


def test_valid_locked_pin(_index: dict | None) -> None:
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
        _index,
    )
    assert errors == []


def test_unknown_component_id(_index: dict | None) -> None:
    errors = _validate_featured(
        "demo",
        _board([{"id": "foo", "component_id": "definitely.not.real", "fields": {}}]),
        {},
        _index,
    )
    assert any("not found in components.index.json" in e for e in errors)


def test_unknown_field_key(_index: dict | None) -> None:
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
        _index,
    )
    assert any("not a config_entry on switch.gpio" in e for e in errors)


def test_pin_not_declared(_index: dict | None) -> None:
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
        _index,
    )
    assert any("GPIO 99 not declared in pins" in e for e in errors)


def test_suggestions_pin_not_declared(_index: dict | None) -> None:
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
        _index,
    )
    assert any("GPIO 99 not declared in pins" in e for e in errors)


def test_dict_pin_with_number_validates(_index: dict | None) -> None:
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
        _index,
    )
    assert errors == []


def _reserved_pins(*gpios: int) -> dict[int, dict]:
    return {g: {"gpio": g, "features": [], "available": False} for g in gpios}


def test_reserved_pin_value_without_lock_is_flagged(_index: dict | None) -> None:
    """A preset value on an available:false pin must be locked or the editor renders it blank."""
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "relay",
                    "component_id": "switch.gpio",
                    "fields": {"pin": {"value": 4}},
                }
            ]
        ),
        _reserved_pins(4),
        _index,
    )
    assert any("GPIO 4 is reserved" in e for e in errors)


def test_reserved_pin_value_locked_passes(_index: dict | None) -> None:
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "relay",
                    "component_id": "switch.gpio",
                    "fields": {"pin": {"value": 4, "locked": True}},
                }
            ]
        ),
        _reserved_pins(4),
        _index,
    )
    assert errors == []


def test_reserved_pin_suggestion_not_flagged(_index: dict | None) -> None:
    """The reserved-must-lock check covers the preset value only, not suggestions."""
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "pir",
                    "component_id": "binary_sensor.gpio",
                    "fields": {"pin": {"suggestions": [4]}},
                }
            ]
        ),
        _reserved_pins(4),
        _index,
    )
    assert errors == []


def test_duplicate_featured_id(_index: dict | None) -> None:
    errors = _validate_featured(
        "demo",
        _board(
            [
                {"id": "relay", "component_id": "switch.gpio", "fields": {}},
                {"id": "relay", "component_id": "switch.gpio", "fields": {}},
            ]
        ),
        {},
        _index,
    )
    assert any("duplicate id 'relay'" in e for e in errors)


def test_duplicate_bundle_id(_index: dict | None) -> None:
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
        _index,
    )
    assert any("duplicate id 'led'" in e for e in errors)


def test_bundle_unknown_component_id(_index: dict | None) -> None:
    errors = _validate_featured(
        "demo",
        _board(
            [{"id": "a", "component_id": "switch.gpio", "fields": {}}],
            [
                {"id": "b", "name": "Bundle", "component_ids": ["a", "ghost"]},
            ],
        ),
        {},
        _index,
    )
    assert any("'ghost' does not match any" in e for e in errors)


def test_locked_and_suggestions_both_set(_index: dict | None) -> None:
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
        _index,
    )
    assert any("cannot set both 'locked' and 'suggestions'" in e for e in errors)


# ---------------------------------------------------------------------------
# is_imported relaxation
# ---------------------------------------------------------------------------


def _pin_entry_requiring(*features: str) -> dict:
    """Build a synthetic ``pin``-typed config entry that demands *features*."""
    return {"key": "pin", "type": "pin", "pin_features": list(features)}


def test_field_preset_imported_skips_pin_feature_check() -> None:
    """Imported boards bypass the pin-feature intersection check."""
    # Synthesised case: the component requires ``adc`` on its pin but
    # the board's synthesized pin entry has empty features (the only
    # shape the importer produces). On a hand-curated board this would
    # error; with ``is_imported=True`` it must pass.
    pins = {3: {"gpio": 3, "features": []}}
    ce = _pin_entry_requiring("adc")
    preset = {"value": 3, "locked": True}

    curated = _validate_field_preset("demo", "pin", preset, ce, pins, is_imported=False)
    assert any("missing required pin features ['adc']" in e for e in curated)

    imported = _validate_field_preset("demo", "pin", preset, ce, pins, is_imported=True)
    assert imported == []


def test_field_preset_imported_skips_reserved_lock_check() -> None:
    """
    Imported boards bypass the reserved-must-lock check.

    The importer synthesizes every pin as ``available: false``, so the check
    would fire on any unlocked pin preset; it polices hand-authored pin maps.
    """
    pins = {3: {"gpio": 3, "features": [], "available": False}}
    ce = _pin_entry_requiring()
    preset = {"value": 3}

    curated = _validate_field_preset("demo", "pin", preset, ce, pins, is_imported=False)
    assert any("GPIO 3 is reserved" in e for e in curated)

    imported = _validate_field_preset("demo", "pin", preset, ce, pins, is_imported=True)
    assert imported == []


def test_field_preset_imported_still_requires_pin_declared() -> None:
    """The pin-declared check stays in effect for imported boards."""
    pins = {3: {"gpio": 3, "features": []}}
    ce = _pin_entry_requiring("adc")
    preset = {"value": 99, "locked": True}  # GPIO99 absent from pins

    errors = _validate_field_preset("demo", "pin", preset, ce, pins, is_imported=True)
    assert any("GPIO 99 not declared in pins" in e for e in errors)


# ---------------------------------------------------------------------------
# id shape + collision checks
# ---------------------------------------------------------------------------


def test_id_with_hyphens_rejected(_index: dict | None) -> None:
    """Hyphens in featured-component ids fail validation outright."""
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "status-led-output",
                    "component_id": "output.gpio",
                    "fields": {},
                }
            ]
        ),
        {},
        _index,
    )
    assert any("no hyphens" in e for e in errors)


def test_id_collides_with_dotted_domain(_index: dict | None) -> None:
    """An id equal to the component_id's domain (e.g. ``output``) is rejected."""
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "output",
                    "component_id": "output.gpio",
                    "fields": {},
                }
            ]
        ),
        {},
        _index,
    )
    assert any("clashes with domain 'output'" in e and "output_<role>" in e for e in errors)


def test_id_collides_with_single_segment_component_id(_index: dict | None) -> None:
    """For component_ids without a dot (``i2c``, ``rtttl``), id must not equal it."""
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "i2c",
                    "component_id": "i2c",
                    "fields": {},
                }
            ]
        ),
        {},
        _index,
    )
    assert any("clashes with domain 'i2c'" in e for e in errors)


def test_clean_id_passes_shape_check(_index: dict | None) -> None:
    """A canonical lowercase-underscore id with a descriptive name passes."""
    errors = _validate_featured(
        "demo",
        _board(
            [
                {
                    "id": "relay_main",
                    "component_id": "switch.gpio",
                    "fields": {},
                }
            ]
        ),
        {},
        _index,
    )
    # Neither shape nor collision messages should appear; the entry is clean.
    assert not any("no hyphens" in e for e in errors)
    assert not any("clashes with domain" in e for e in errors)


def test_bundle_id_with_hyphens_rejected(_index: dict | None) -> None:
    """The same shape rule applies to ``featured_bundles[].id``."""
    errors = _validate_featured(
        "demo",
        _board(
            [{"id": "a", "component_id": "switch.gpio", "fields": {}}],
            [{"id": "rgb-buzzer", "name": "RGB+Buzzer", "component_ids": ["a"]}],
        ),
        {},
        _index,
    )
    assert any("no hyphens" in e and "rgb-buzzer" in e for e in errors)


def test_default_components_accepts_featured_component_ref(_index: dict | None) -> None:
    """A default_components entry matching a featured_components.id passes."""
    errors = _validate_featured(
        "demo",
        _board(
            [{"id": "relay", "component_id": "switch.gpio", "fields": {}}],
            defaults=["relay"],
        ),
        {},
        _index,
    )
    assert errors == []


def test_default_components_accepts_catalog_component_id(_index: dict | None) -> None:
    """A bare catalog id (e.g. ``web_server``) passes the fall-through path."""
    errors = _validate_featured("demo", _board(defaults=["web_server"]), {}, _index)
    assert errors == []


def test_default_components_rejects_unknown_ref(_index: dict | None) -> None:
    """An unknown id (neither featured nor catalog) is flagged."""
    errors = _validate_featured("demo", _board(defaults=["bogus_id"]), {}, _index)
    assert any("default_components" in e and "bogus_id" in e for e in errors), (
        f"expected a default_components error, got {errors!r}"
    )


def test_default_components_object_form_accepts_inline_fields(_index: dict | None) -> None:
    """The ``{id, fields}`` shape is valid and the id is still cross-checked."""
    errors = _validate_featured(
        "demo",
        _board(defaults=[{"id": "web_server", "fields": {"version": "3"}}]),
        {},
        _index,
    )
    assert errors == []


def test_default_components_object_form_rejects_unknown_id(_index: dict | None) -> None:
    """Object form with an unknown ``id`` is flagged identically to the string form."""
    errors = _validate_featured(
        "demo",
        _board(defaults=[{"id": "bogus_id", "fields": {"x": 1}}]),
        {},
        _index,
    )
    assert any("bogus_id" in e for e in errors)


def test_default_components_object_form_missing_id_flagged(_index: dict | None) -> None:
    """Object form without an ``id`` produces a clear error."""
    errors = _validate_featured(
        "demo",
        _board(defaults=[{"fields": {"x": 1}}]),
        {},
        _index,
    )
    assert any("missing 'id'" in e for e in errors)


def test_featured_dep_unsatisfied_bus_is_flagged(_index: dict | None) -> None:
    """An imported board featuring an i2c sensor with no i2c bus is flagged."""
    errors = _validate_featured_dependencies("demo", [_leaf("sensor.sht3xd", "t")], _index, True)
    assert any("depends on bus 'i2c'" in e for e in errors)


def test_featured_dep_satisfied_by_featured_bus(_index: dict | None) -> None:
    """The board passes once the i2c bus is also featured."""
    featured = [_leaf("i2c", "bus"), _leaf("sensor.sht3xd", "t")]
    assert _validate_featured_dependencies("demo", featured, _index, True) == []


def test_featured_dep_platform_bus_satisfied_by_domain(_index: dict | None) -> None:
    """A one_wire dep is satisfied by a featured one_wire.gpio (domain match)."""
    featured = [_leaf("one_wire.gpio", "ow"), _leaf("sensor.dallas_temp", "t")]
    assert _validate_featured_dependencies("demo", featured, _index, True) == []


def test_featured_dep_not_checked_for_non_imported(_index: dict | None) -> None:
    """Curated (non-imported) boards are exempt; their featured set is à la carte."""
    assert (
        _validate_featured_dependencies("demo", [_leaf("sensor.sht3xd", "t")], _index, False) == []
    )


def test_featured_dep_allowlisted_pair_is_skipped(
    _index: dict | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An allow-listed ``(board, bus)`` pair's known-unsatisfied dep does not fail."""
    monkeypatch.setattr(
        "script.validate_definitions._UNSATISFIED_BUS_ALLOW_LIST", frozenset({("demo", "i2c")})
    )
    assert (
        _validate_featured_dependencies("demo", [_leaf("sensor.sht3xd", "t")], _index, True) == []
    )


def test_featured_dep_allowlist_is_per_bus_not_per_board(
    _index: dict | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An allow-listed pair waives only that bus; a different bus on the board still fails."""
    monkeypatch.setattr(
        "script.validate_definitions._UNSATISFIED_BUS_ALLOW_LIST", frozenset({("demo", "i2c")})
    )
    # demo is waived for i2c; an spi leaf with no spi bus must still fail.
    errors = _validate_featured_dependencies("demo", [_leaf("display.mipi_spi", "d")], _index, True)
    assert any("depends on bus 'spi'" in e for e in errors)


def test_featured_dep_satisfied_by_default_component(_index: dict | None) -> None:
    """A bus provided through default_components counts as present."""
    errors = _validate_featured_dependencies(
        "demo", [_leaf("sensor.sht3xd", "t")], _index, True, defaults=["i2c"]
    )
    assert errors == []


def test_featured_dep_dual_mode_top_level_hub_not_flagged(_index: dict | None) -> None:
    """A bare dual-mode hub (sn74hc595 bit-banged over GPIO) is not flagged for spi."""
    # sn74hc595 declares dependencies: ["spi"] but here drives GPIO pins, so the
    # bus dep is conditional; the platform-leaf scoping intentionally skips it
    # rather than false-positive on every dual-mode hub.
    featured = [{"id": "sr", "component_id": "sn74hc595", "fields": {"data_pin": {"value": 1}}}]
    assert _validate_featured_dependencies("demo", featured, _index, True) == []


def test_featured_dep_non_string_component_id_does_not_crash(_index: dict | None) -> None:
    """A malformed non-string component_id is skipped (no unhashable-type crash)."""
    featured = [{"id": "x", "component_id": ["bad"]}, _leaf("sensor.sht3xd", "t")]
    errors = _validate_featured_dependencies("demo", featured, _index, True)
    assert any("depends on bus 'i2c'" in e for e in errors)
