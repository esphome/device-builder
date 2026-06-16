"""Tests for suppressing YAML-only components' (lvgl) catalog body."""

from __future__ import annotations

from script.sync_components import (  # type: ignore[import-not-found]
    _YAML_ONLY_COMPONENT_IDS,
    _suppress_yaml_only_body,
)


def test_lvgl_is_yaml_only() -> None:
    """Lvgl ships YAML-only, so its ~14 MB body is suppressed."""
    assert "lvgl" in _YAML_ONLY_COMPONENT_IDS


def test_suppress_drops_config_entries_for_yaml_only_component() -> None:
    """A YAML-only component's config_entries are emptied; other fields stay."""
    out = _suppress_yaml_only_body(
        {"id": "lvgl", "name": "LVGL", "config_entries": [{"key": "pages"}]}
    )
    assert out["config_entries"] == []
    assert out["name"] == "LVGL"


def test_suppress_leaves_normal_component_untouched() -> None:
    """A component with a structured form keeps its config_entries."""
    out = _suppress_yaml_only_body({"id": "sensor.dht", "config_entries": [{"key": "pin"}]})
    assert out["config_entries"] == [{"key": "pin"}]
