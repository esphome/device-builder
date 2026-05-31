"""Pin the catalog's component-level ``is_list`` flag and flat entries."""

from __future__ import annotations

import json
from pathlib import Path

from esphome_device_builder.models import ComponentCatalogEntry

_COMPONENTS_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)


def _load(component_id: str) -> ComponentCatalogEntry:
    raw = json.loads((_COMPONENTS_DIR / f"{component_id}.json").read_text())
    return ComponentCatalogEntry.from_dict(raw)


def test_globals_body_is_marked_as_list() -> None:
    assert _load("globals").is_list is True


def test_other_list_bodied_components_are_marked() -> None:
    # globals is not special-cased; the flag rides on every list-bodied
    # component the schema marks (i2c, font, ...).
    assert _load("i2c").is_list is True
    assert _load("font").is_list is True


def test_globals_config_entries_stay_flat() -> None:
    # Flat fields, not a single nested wrapper, so add-component is untouched.
    entry = _load("globals")
    keys = {e.key for e in entry.config_entries}
    assert {"id", "initial_value"} <= keys
    assert len(entry.config_entries) >= 2


def test_single_mapping_component_is_not_list() -> None:
    assert _load("logger").is_list is False
