"""Pin the top-level ``psram:`` lift and its ``requires`` stitching."""

from __future__ import annotations

from typing import Any

import pytest

import script.sync_esphome_devices as sync  # type: ignore[import-not-found]
from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _extract_psram,
    _fold_requires_into_bundles,
    _lift_psram,
    _psram_allocating_components,
)

_COMPONENTS: dict[str, dict[str, Any]] = {
    "psram": {
        "category": "misc",
        "config_entries": [
            {"key": "mode", "type": "string"},
            {"key": "speed", "type": "string"},
        ],
    },
    "display.st7701s": {"category": "display", "config_entries": []},
    "speaker.i2s_audio": {"category": "speaker", "config_entries": []},
    "display.mipi_rgb": {
        "category": "display",
        "dependencies": ["psram"],
        "config_entries": [],
    },
    "switch.gpio": {"category": "switch", "config_entries": []},
}


@pytest.fixture(autouse=True)
def _pin_psram_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the source-scan result so tests don't depend on the installed esphome."""
    monkeypatch.setattr(
        sync, "_psram_allocating_components", lambda: frozenset({"display", "i2s_audio"})
    )


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


def test_unliftable_psram_block_warns(caplog: pytest.LogCaptureFixture) -> None:
    """A configured ``psram:`` block that produces no lift is loud, not silent."""
    featured = [_display_entry()]
    assert _lift_psram({"psram": {"mode": "(FILL IN MODE)"}}, featured, _COMPONENTS) == featured
    assert "can't be represented" in caplog.text
    caplog.clear()
    assert _lift_psram({"logger": None}, featured, _COMPONENTS) == featured
    assert caplog.text == ""


def test_psram_allocating_entries_gain_requires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Domain-dir, platform-dir, and psram-dep leaves get the stamp; a relay doesn't."""
    display = _display_entry()
    display["requires"] = ["lcd_spi"]
    speaker = {"id": "spkr", "component_id": "speaker.i2s_audio", "fields": {"id": "spkr"}}
    by_dep = {"id": "rgb", "component_id": "display.mipi_rgb", "fields": {"id": "rgb"}}
    switch = {"id": "relay", "component_id": "switch.gpio", "fields": {"id": "relay"}}
    # by_dep must stamp through its declared dependency even with no scan hit.
    monkeypatch.setattr(sync, "_psram_allocating_components", lambda: frozenset({"i2s_audio"}))
    featured = _lift_psram(
        {"psram": {"mode": "octal"}}, [display, speaker, by_dep, switch], _COMPONENTS
    )
    assert featured[0]["id"] == "onboard_psram"
    assert display["requires"] == ["lcd_spi"]
    assert speaker["requires"] == ["onboard_psram"]
    assert by_dep["requires"] == ["onboard_psram"]
    assert "requires" not in switch

    monkeypatch.setattr(sync, "_psram_allocating_components", lambda: frozenset({"display"}))
    _lift_psram({"psram": {"mode": "octal"}}, [display], _COMPONENTS)
    assert display["requires"] == ["lcd_spi", "onboard_psram"]


def test_psram_allocating_components_scans_installed_esphome() -> None:
    """The real source scan finds the known PSRAM allocators (needs esphome installed)."""
    pytest.importorskip("esphome")
    allocating = _psram_allocating_components()
    assert {"display", "i2s_audio", "micro_wake_word"} <= allocating
    assert "gpio" not in allocating


def test_requires_folds_into_full_setup_bundle() -> None:
    """The stitched prerequisite lands in the display's bundle ahead of its members."""
    featured = _lift_psram({"psram": {"mode": "octal"}}, [_display_entry()], _COMPONENTS)
    bundles = [{"id": "tft_setup", "name": "Display", "component_ids": ["tft_display"]}]
    _fold_requires_into_bundles(bundles, featured)
    assert bundles[0]["component_ids"] == ["onboard_psram", "tft_display"]
