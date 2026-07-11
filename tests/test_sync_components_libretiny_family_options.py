"""Each libretiny platform's ``family`` options list only its own chips."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _apply_libretiny_family_options,
)

_BODIES_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)

_EXPECTED_FAMILIES = {
    "bk72xx": ["BK7231N", "BK7231Q", "BK7231T", "BK7238", "BK7251"],
    "rtl87xx": ["RTL8710B", "RTL8720C"],
    "ln882x": ["LN882H"],
}

_ALL_FAMILY_OPTIONS = [
    {"label": f, "value": f}
    for f in (
        "BK7231N",
        "BK7231Q",
        "BK7231T",
        "BK7238",
        "BK7251",
        "LN882H",
        "RTL8710B",
        "RTL8720C",
    )
]


def _platform_entry(component_id: str) -> dict:
    return {
        "id": component_id,
        "config_entries": [
            {"key": "board", "type": "string"},
            {"key": "family", "type": "string", "options": [dict(o) for o in _ALL_FAMILY_OPTIONS]},
        ],
    }


def _family_values(entry: dict) -> list[str]:
    (field,) = [f for f in entry["config_entries"] if f["key"] == "family"]
    return [o["value"] for o in field["options"]]


@pytest.mark.parametrize(("component_id", "expected"), sorted(_EXPECTED_FAMILIES.items()))
def test_family_options_narrowed_per_platform(component_id: str, expected: list[str]) -> None:
    entry = _platform_entry(component_id)
    _apply_libretiny_family_options([entry])
    assert _family_values(entry) == expected


def test_non_libretiny_entry_untouched() -> None:
    entry = _platform_entry("esp32")
    _apply_libretiny_family_options([entry])
    assert _family_values(entry) == [o["value"] for o in _ALL_FAMILY_OPTIONS]


def test_unrecognised_options_fail_loud() -> None:
    entry = {
        "id": "bk72xx",
        "config_entries": [
            {"key": "family", "type": "string", "options": [{"label": "X", "value": "X"}]}
        ],
    }
    with pytest.raises(RuntimeError, match="bk72xx"):
        _apply_libretiny_family_options([entry])


@pytest.mark.parametrize(("component_id", "expected"), sorted(_EXPECTED_FAMILIES.items()))
def test_committed_bodies_pin_family_options(component_id: str, expected: list[str]) -> None:
    body = json.loads((_BODIES_DIR / f"{component_id}.json").read_text(encoding="utf-8"))
    assert _family_values(body) == expected
