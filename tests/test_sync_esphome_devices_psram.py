"""Pin the top-level ``psram:`` lift and its display ``requires`` stitching."""

from __future__ import annotations

from typing import Any

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _extract_psram,
    _fold_requires_into_bundles,
    _lift_psram,
)

_COMPONENTS: dict[str, dict[str, Any]] = {
    "psram": {
        "category": "misc",
        "config_entries": [
            {"key": "mode", "type": "string"},
            {"key": "speed", "type": "string"},
        ],
    },
}


def _display_entry() -> dict[str, Any]:
    return {
        "id": "tft_display",
        "component_id": "display.st7701s",
        "fields": {"id": "tft_display"},
    }


def test_lifts_psram_block_fields() -> None:
    """A configured ``psram:`` block lifts with its scalar fields as suggestions."""
    entry = _extract_psram({"psram": {"mode": "octal", "speed": "80MHz"}}, _COMPONENTS)
    assert entry == {
        "id": "onboard_psram",
        "component_id": "psram",
        "name": "PSRAM",
        "fields": {"mode": "octal", "speed": "80MHz"},
    }


def test_lifts_bare_psram_key() -> None:
    """A bare ``psram:`` (null body) lifts fieldless."""
    entry = _extract_psram({"psram": None}, _COMPONENTS)
    assert entry is not None
    assert entry["fields"] == {}


def test_no_entry_without_psram_key() -> None:
    assert _extract_psram({"logger": None}, _COMPONENTS) is None


def test_placeholder_psram_skips_lift() -> None:
    """A fill-me-in sentinel distrusts the whole block."""
    assert _extract_psram({"psram": {"mode": "(FILL IN MODE)"}}, _COMPONENTS) is None


def test_psram_missing_from_catalog_is_a_noop() -> None:
    assert _extract_psram({"psram": {"mode": "octal"}}, {}) is None


def test_not_lifted_on_otherwise_empty_board() -> None:
    """A psram-only page must not become importable through the lift."""
    assert _lift_psram({"psram": {"mode": "octal"}}, [], _COMPONENTS) == []


def test_display_entries_gain_psram_requires() -> None:
    """The lift prepends the entry and marks it a display prerequisite, merging requires."""
    display = _display_entry()
    display["requires"] = ["lcd_spi"]
    switch = {"id": "relay", "component_id": "switch.gpio", "fields": {"id": "relay"}}
    featured = _lift_psram({"psram": {"mode": "octal"}}, [display, switch], _COMPONENTS)
    assert featured[0]["id"] == "onboard_psram"
    assert display["requires"] == ["lcd_spi", "onboard_psram"]
    assert "requires" not in switch


def test_requires_folds_into_full_setup_bundle() -> None:
    """The stitched prerequisite lands in the display's bundle ahead of its members."""
    featured = _lift_psram({"psram": {"mode": "octal"}}, [_display_entry()], _COMPONENTS)
    bundles = [{"id": "tft_setup", "name": "Display", "component_ids": ["tft_display"]}]
    _fold_requires_into_bundles(bundles, featured)
    assert bundles[0]["component_ids"] == ["onboard_psram", "tft_display"]
