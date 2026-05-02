"""Tests for the featured-components feature.

Covers four layers:

1. Loader — primitive shorthand, locked, suggestions, dict pin shape, and
   the in-manifest mutual-exclusion rules.
2. Featured registry — IDs are minted as ``featured.<board>.<local>``,
   unknown component_ids are skipped with a warning rather than crashing
   the load.
3. Materialisation — ``locked`` and ``suggestions`` ride through to the
   returned ``ConfigEntry`` and ``default_value`` reflects the preset.
4. Add-component flow — ``_apply_featured_presets`` enforces the locked
   and suggestion rules and lets plain defaults fall through.
"""

from __future__ import annotations

from typing import Any

import pytest

from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.controllers.components import ComponentCatalog
from esphome_device_builder.controllers.devices import _apply_featured_presets
from esphome_device_builder.definitions import (
    _coerce_field_preset,
    _load_featured_bundle,
    _load_featured_component,
)
from esphome_device_builder.models import ComponentCategory

# ---------------------------------------------------------------------------
# Loader-level (pure unit tests, no catalog)
# ---------------------------------------------------------------------------


def test_coerce_primitive_shorthand() -> None:
    """Bare primitives become FieldPreset(value=x), not locked."""
    preset = _coerce_field_preset(12)
    assert preset.value == 12
    assert preset.locked is False
    assert preset.suggestions is None


def test_coerce_locked_form() -> None:
    """Verbose dict with locked=True passes locked through."""
    preset = _coerce_field_preset({"value": 12, "locked": True})
    assert preset.value == 12
    assert preset.locked is True
    assert preset.suggestions is None


def test_coerce_suggestions_form() -> None:
    """``suggestions`` populates the picker; value can come along as initial."""
    preset = _coerce_field_preset({"suggestions": [4, 5], "value": 4})
    assert preset.value == 4
    assert preset.locked is False
    assert preset.suggestions == [4, 5]


def test_coerce_dict_pin_value() -> None:
    """Rich pin form (mapping) survives as the preset value."""
    rich = {"number": 0, "mode": {"input": True, "pullup": True}, "inverted": True}
    preset = _coerce_field_preset({"value": rich, "locked": True})
    assert preset.value == rich
    assert preset.locked is True


def test_load_featured_component_minimal() -> None:
    """Only id+component_id required; fields default to empty."""
    fc = _load_featured_component({"id": "dht", "component_id": "sensor.dht"})
    assert fc.id == "dht"
    assert fc.component_id == "sensor.dht"
    assert fc.fields == {}


def test_load_featured_bundle() -> None:
    """Bundle just stores ids — uniqueness/cross-refs come at validate time."""
    fb = _load_featured_bundle(
        {
            "id": "status-led",
            "name": "Status LED",
            "description": "...",
            "component_ids": ["status-led-output", "status-led-light"],
        }
    )
    assert fb.id == "status-led"
    assert fb.component_ids == ["status-led-output", "status-led-light"]


# ---------------------------------------------------------------------------
# Registry & materialisation (real catalogs)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def catalog() -> ComponentCatalog:
    """Boot board + component catalogs once per module."""

    class _DB:
        boards: BoardCatalog | None = None
        components: ComponentCatalog | None = None

    db = _DB()
    db.boards = BoardCatalog()
    db.boards.load()
    db.components = ComponentCatalog(db)
    db.components.load()
    return db.components


def test_registry_indexes_known_boards(catalog: ComponentCatalog) -> None:
    """Tier-1 manifests register their featured components under the right ids."""
    assert "featured.sonoff-basic.relay" in catalog._featured_by_id
    assert "featured.apollo-esk-1.pir-motion" in catalog._featured_by_id
    assert "featured.athom-smart-plug-v3.relay" in catalog._featured_by_id


def test_registry_groups_per_board(catalog: ComponentCatalog) -> None:
    """``_featured_by_board`` lets get_components scope the featured listing."""
    assert "featured.sonoff-basic.relay" in catalog._featured_by_board["sonoff-basic"]
    assert all(
        bid.startswith("featured.apollo-esk-1.")
        for bid in catalog._featured_by_board["apollo-esk-1"]
    )


async def test_get_component_locked_field(catalog: ComponentCatalog) -> None:
    """Sonoff relay materialisation pins ``pin`` to GPIO12 and marks it locked."""
    entry = await catalog.get_component(component_id="featured.sonoff-basic.relay")
    assert entry is not None
    assert entry.id == "featured.sonoff-basic.relay"
    assert entry.category == ComponentCategory.FEATURED
    assert entry.name == "Onboard Relay"
    pin = next(ce for ce in entry.config_entries if ce.key == "pin")
    assert pin.default_value == 12
    assert pin.locked is True
    assert pin.suggestions is None


async def test_get_component_suggestions(catalog: ComponentCatalog) -> None:
    """ESK-1 PIR materialisation surfaces the pin suggestions list."""
    entry = await catalog.get_component(component_id="featured.apollo-esk-1.pir-motion")
    assert entry is not None
    pin = next(ce for ce in entry.config_entries if ce.key == "pin")
    assert pin.default_value == 4
    assert pin.locked is False
    assert pin.suggestions == [4, 5]


async def test_get_components_featured_only_with_board_id(
    catalog: ComponentCatalog,
) -> None:
    """``category=featured`` returns the per-board recommended list."""
    page = await catalog.get_components(board_id="sonoff-basic", category="featured")
    ids = {c.id for c in page.components}
    assert "featured.sonoff-basic.relay" in ids
    assert all(c.category == ComponentCategory.FEATURED for c in page.components)


async def test_get_components_excludes_featured_by_default(
    catalog: ComponentCatalog,
) -> None:
    """A regular catalog query never includes featured entries."""
    page = await catalog.get_components(board_id="sonoff-basic", limit=2000)
    assert all(not c.id.startswith("featured.") for c in page.components)


async def test_get_components_mixed_category_unions(
    catalog: ComponentCatalog,
) -> None:
    """``category=[featured, sensor]`` returns featured first then matching sensors."""
    page = await catalog.get_components(
        board_id="sonoff-basic",
        category=["featured", "sensor"],
        limit=2000,
    )
    categories_seen = {c.category for c in page.components}
    assert ComponentCategory.FEATURED in categories_seen
    assert ComponentCategory.SENSOR in categories_seen
    first_non_featured = next(
        (i for i, c in enumerate(page.components) if c.category != ComponentCategory.FEATURED),
        len(page.components),
    )
    assert all(
        c.category == ComponentCategory.FEATURED for c in page.components[:first_non_featured]
    )


async def test_get_component_featured_ignores_mismatched_board_id(
    catalog: ComponentCatalog,
) -> None:
    """Featured ids resolve their platform from ``record.board_id``, not the caller's."""
    entry = await catalog.get_component(
        component_id="featured.sonoff-basic.relay",
        board_id="apollo-esk-1",
    )
    assert entry is not None
    assert entry.id == "featured.sonoff-basic.relay"


async def test_get_component_unknown_featured_id(catalog: ComponentCatalog) -> None:
    """Unknown ``featured.*`` ids return ``None`` instead of raising."""
    assert await catalog.get_component(component_id="featured.no-such-board.x") is None


async def test_get_components_featured_with_query_filter(
    catalog: ComponentCatalog,
) -> None:
    """``query`` narrows the featured listing on name / description / id."""
    page = await catalog.get_components(
        board_id="apollo-esk-1",
        category="featured",
        query="pir",
    )
    assert any("pir" in c.id.lower() for c in page.components)
    assert all(
        "pir" in c.name.lower() or "pir" in c.description.lower() or "pir" in c.id.lower()
        for c in page.components
    )


async def test_get_categories_surfaces_featured_count(
    catalog: ComponentCatalog,
) -> None:
    """``board_id`` makes the synthetic ``featured`` category appear."""
    cats = await catalog.get_categories(board_id="apollo-esk-1")
    featured = next(c for c in cats if c["id"] == "featured")
    assert int(featured["count"]) == len(catalog._featured_by_board["apollo-esk-1"])


async def test_get_categories_no_featured_without_board(
    catalog: ComponentCatalog,
) -> None:
    """Without ``board_id`` we don't synthesise the ``featured`` row."""
    cats = await catalog.get_categories()
    assert all(c["id"] != "featured" for c in cats)


# ---------------------------------------------------------------------------
# Add-path preset application
# ---------------------------------------------------------------------------


async def test_apply_presets_locked_fills_in(catalog: ComponentCatalog) -> None:
    """Empty user input picks up the locked + default values from the preset."""
    record = catalog.get_featured_record("featured.sonoff-basic.relay")
    assert record is not None
    out = _apply_featured_presets(record, {})
    assert out["pin"] == 12
    assert out["name"] == "Relay"


async def test_apply_presets_locked_rejects_override(
    catalog: ComponentCatalog,
) -> None:
    """Submitting a different value for a locked field raises ValueError."""
    record = catalog.get_featured_record("featured.sonoff-basic.relay")
    assert record is not None
    with pytest.raises(ValueError, match="locked"):
        _apply_featured_presets(record, {"pin": 5})


async def test_apply_presets_locked_accepts_matching_value(
    catalog: ComponentCatalog,
) -> None:
    """Submitting the exact locked value is allowed (idempotent)."""
    record = catalog.get_featured_record("featured.sonoff-basic.relay")
    assert record is not None
    out = _apply_featured_presets(record, {"pin": 12, "name": "MyRelay"})
    assert out["pin"] == 12
    assert out["name"] == "MyRelay"  # plain default is overridable


async def test_apply_presets_suggestion_in_set(catalog: ComponentCatalog) -> None:
    record = catalog.get_featured_record("featured.apollo-esk-1.pir-motion")
    assert record is not None
    out = _apply_featured_presets(record, {"pin": 5})
    assert out["pin"] == 5
    assert out["device_class"] == "motion"


async def test_apply_presets_suggestion_rejects_off_list(
    catalog: ComponentCatalog,
) -> None:
    record = catalog.get_featured_record("featured.apollo-esk-1.pir-motion")
    assert record is not None
    with pytest.raises(ValueError, match="must be one of"):
        _apply_featured_presets(record, {"pin": 99})


async def test_apply_presets_suggestion_falls_back_to_value(
    catalog: ComponentCatalog,
) -> None:
    """Omitting a suggestion field falls back to the preset's initial value."""
    record = catalog.get_featured_record("featured.apollo-esk-1.pir-motion")
    assert record is not None
    out = _apply_featured_presets(record, {})
    assert out["pin"] == 4


async def test_apply_presets_default_overridable(catalog: ComponentCatalog) -> None:
    """Plain defaults (no locked/suggestions) are overridable by user input."""
    record = catalog.get_featured_record("featured.apollo-esk-1.aht20")
    assert record is not None
    out: dict[str, Any] = _apply_featured_presets(record, {"variant": "AHT10"})
    assert out["variant"] == "AHT10"


async def test_apply_presets_locked_without_value_fails_fast(
    catalog: ComponentCatalog,
) -> None:
    """A malformed manifest (locked=True with no value) fails fast at add time."""
    from copy import deepcopy

    from esphome_device_builder.models import FieldPreset

    record = deepcopy(catalog.get_featured_record("featured.sonoff-basic.relay"))
    assert record is not None
    record.featured.fields["pin"] = FieldPreset(value=None, locked=True)
    with pytest.raises(ValueError, match="locked=true without a value"):
        _apply_featured_presets(record, {})
