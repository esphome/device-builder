"""Catalog carries the component-level ``is_list`` flag.

ESPHome's raw schema marks list-bodied components (their YAML body is a
list of mappings, e.g. ``globals``) with ``CONFIG_SCHEMA.is_list``. The
catalog used to drop it, so the section editor could not tell a list body
from a single mapping. These pin the shipped artifact + model field:
``is_list`` rides on the detail body, ``config_entries`` stay flat so the
add-component path reads one item's fields unchanged.
"""

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


def test_globals_config_entries_stay_flat() -> None:
    # is_list is metadata only; a single-mapping wrapper would collapse
    # to one nested entry. The flat field set must survive so the
    # add-component round-trip is untouched.
    entry = _load("globals")
    keys = {e.key for e in entry.config_entries}
    assert {"id", "initial_value"} <= keys
    assert len(entry.config_entries) >= 2


def test_single_mapping_component_is_not_list() -> None:
    # ``logger`` body is a single mapping; the flag is stripped from the
    # artifact and defaults to False on the model.
    assert _load("logger").is_list is False
