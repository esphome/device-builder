"""``esphome.areas`` lives under Advanced with ``devices``; ``area`` stays main-form."""

from __future__ import annotations

import json
from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    _classify_advanced,
)

_BODIES_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)


def _esphome_entries() -> dict[str, dict]:
    body = json.loads((_BODIES_DIR / "esphome.json").read_text(encoding="utf-8"))
    return {entry["key"]: entry for entry in body["config_entries"]}


def test_areas_classifies_advanced() -> None:
    assert _classify_advanced("areas", required=False, is_structural=False) is True


def test_area_stays_main_form() -> None:
    assert _classify_advanced("area", required=False, is_structural=False) is False


def test_committed_body_pins_areas_under_advanced() -> None:
    entries = _esphome_entries()
    assert entries["areas"]["advanced"] is True
    assert entries["devices"]["advanced"] is True
    assert not entries["area"].get("advanced")
