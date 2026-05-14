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

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from esphome_device_builder.controllers.components import ComponentCatalog
from esphome_device_builder.controllers.devices import DevicesController
from esphome_device_builder.controllers.devices._state import DevicesState
from esphome_device_builder.controllers.devices.helpers import (
    _apply_featured_presets,
    _drop_unconfigured_dependent_fields,
)
from esphome_device_builder.definitions import (
    _coerce_field_preset,
    _load_featured_bundle,
    _load_featured_component,
)
from esphome_device_builder.helpers.yaml import generate_component_yaml
from esphome_device_builder.models import ComponentCategory
from esphome_device_builder.models.common import FieldPreset

# Pin every test in the file onto the same xdist worker as the rest of
# the catalog-heavy suite so they share one ``ComponentCatalog.load``
# instead of each worker paying ~2s on Linux CI. The unit tests at the
# top of the file don't touch the catalog but it's not worth splitting
# the file for the savings.
pytestmark = pytest.mark.xdist_group("catalog")

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
            "id": "status_led",
            "name": "Status LED",
            "description": "...",
            "component_ids": ["status_led_output", "status_led_light"],
        }
    )
    assert fb.id == "status_led"
    assert fb.component_ids == ["status_led_output", "status_led_light"]


# ---------------------------------------------------------------------------
# Registry & materialisation (real catalogs)
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog(session_component_catalog: ComponentCatalog) -> ComponentCatalog:
    """Reuse the session-scoped catalog (board catalog already wired in)."""
    return session_component_catalog


def test_registry_indexes_known_boards(catalog: ComponentCatalog) -> None:
    """Tier-1 manifests register their featured components under the right ids."""
    assert "featured.sonoff-basic.relay" in catalog._featured_by_id
    assert "featured.apollo-esk-1.motion_module" in catalog._featured_by_id
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
    """Materialisation rides ``preset.suggestions`` onto the returned ConfigEntry."""
    # No live board manifest currently sets ``suggestions:`` (the
    # apollo-esk-1 starter kit moved to fixed pin assignments), so we
    # swap a synthetic record into the catalog for the duration of the
    # test to exercise the full materialisation path.
    full_id = "featured.apollo-esk-1.motion_module"
    original = catalog._featured_by_id[full_id]
    patched = deepcopy(original)
    patched.featured.fields["pin"] = FieldPreset(value=4, suggestions=[4, 5])
    catalog._featured_by_id[full_id] = patched
    try:
        entry = await catalog.get_component(component_id=full_id)
    finally:
        catalog._featured_by_id[full_id] = original
    assert entry is not None
    pin = next(ce for ce in entry.config_entries if ce.key == "pin")
    assert pin.default_value == 4
    assert pin.locked is False
    assert pin.suggestions == [4, 5]


async def test_get_component_id_from_manifest_field(catalog: ComponentCatalog) -> None:
    """A featured component's ``fields.id`` preset surfaces as the materialised id default."""
    entry = await catalog.get_component(component_id="featured.athom-smart-plug-v3.button")
    assert entry is not None
    id_field = next(ce for ce in entry.config_entries if ce.key == "id")
    assert id_field.default_value == "button"
    assert id_field.locked is False


async def test_get_component_name_from_manifest_field(
    catalog: ComponentCatalog,
) -> None:
    """A featured component's ``fields.name`` preset surfaces as the materialised name default."""
    # sonoff-basic.relay has ``fields.name: Relay`` in the manifest;
    # the materialised view exposes that as the underlying switch.gpio
    # ``name`` config_entry's default.
    entry = await catalog.get_component(component_id="featured.sonoff-basic.relay")
    assert entry is not None
    name_field = next(ce for ce in entry.config_entries if ce.key == "name")
    assert name_field.default_value == "Relay"
    assert name_field.locked is False


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
        query="motion",
    )
    assert any("motion" in c.id.lower() for c in page.components)
    assert all(
        "motion" in c.name.lower() or "motion" in c.description.lower() or "motion" in c.id.lower()
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


async def test_get_components_response_categories_filter_featured_by_query(
    catalog: ComponentCatalog,
) -> None:
    """
    The synthetic ``featured`` sidebar bucket tracks the query.

    Present when at least one featured component matches, absent
    otherwise.
    """
    page = await catalog.get_components(
        board_id="apollo-esk-1",
        query="zzz-no-such-featured-component",
    )
    assert all(c["id"] != "featured" for c in page.categories)

    page = await catalog.get_components(board_id="apollo-esk-1", query="motion")
    featured = next(c for c in page.categories if c["id"] == "featured")
    assert int(featured["count"]) >= 1


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
    # No live board manifest currently sets ``suggestions:`` — the
    # apollo-esk-1 starter kit moved to fixed pin assignments — so the
    # suggestion-logic tests build their fixture inline by overriding
    # the ``pin`` preset on a deepcopy of a real record.
    record = deepcopy(catalog.get_featured_record("featured.apollo-esk-1.motion_module"))
    assert record is not None
    record.featured.fields["pin"] = FieldPreset(value=4, suggestions=[4, 5])
    out = _apply_featured_presets(record, {"pin": 5})
    assert out["pin"] == 5
    assert out["device_class"] == "motion"


async def test_apply_presets_suggestion_rejects_off_list(
    catalog: ComponentCatalog,
) -> None:
    record = deepcopy(catalog.get_featured_record("featured.apollo-esk-1.motion_module"))
    assert record is not None
    record.featured.fields["pin"] = FieldPreset(value=4, suggestions=[4, 5])
    with pytest.raises(ValueError, match="must be one of"):
        _apply_featured_presets(record, {"pin": 99})


async def test_apply_presets_suggestion_accepts_rich_pin_form(
    catalog: ComponentCatalog,
) -> None:
    """
    Frontend submits pin fields as the rich ``{number, mode, ...}`` shape.

    The suggestion check must compare on the GPIO number so a
    preset's ``suggestions: [4, 5]`` accepts ``{"number": 5, ...}`` too
    — and the rich dict rides through to the merger unchanged so the
    YAML keeps its full pin block.
    """
    record = deepcopy(catalog.get_featured_record("featured.apollo-esk-1.motion_module"))
    assert record is not None
    record.featured.fields["pin"] = FieldPreset(value=4, suggestions=[4, 5])
    rich_pin = {"number": 5, "mode": {"input": True}}
    out = _apply_featured_presets(record, {"pin": rich_pin})
    assert out["pin"] == rich_pin


async def test_apply_presets_suggestion_rejects_rich_pin_off_list(
    catalog: ComponentCatalog,
) -> None:
    """Rich pin form whose ``number`` is off-list still raises."""
    record = deepcopy(catalog.get_featured_record("featured.apollo-esk-1.motion_module"))
    assert record is not None
    record.featured.fields["pin"] = FieldPreset(value=4, suggestions=[4, 5])
    with pytest.raises(ValueError, match="must be one of"):
        _apply_featured_presets(record, {"pin": {"number": 99, "mode": {"input": True}}})


async def test_apply_presets_locked_accepts_rich_pin_form(
    catalog: ComponentCatalog,
) -> None:
    """A bare-int locked pin must also accept the rich-form echo from the frontend."""
    record = catalog.get_featured_record("featured.sonoff-basic.relay")
    assert record is not None
    rich_pin = {"number": 12, "mode": {"output": True}}
    out = _apply_featured_presets(record, {"pin": rich_pin})
    # Locked wins — the merged value is the manifest's bare GPIO, not the
    # frontend's rich echo (the locked branch always replaces the value).
    assert out["pin"] == 12


async def test_apply_presets_suggestion_falls_back_to_value(
    catalog: ComponentCatalog,
) -> None:
    """Omitting a suggestion field falls back to the preset's initial value."""
    record = deepcopy(catalog.get_featured_record("featured.apollo-esk-1.motion_module"))
    assert record is not None
    record.featured.fields["pin"] = FieldPreset(value=4, suggestions=[4, 5])
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
    record = deepcopy(catalog.get_featured_record("featured.sonoff-basic.relay"))
    assert record is not None
    record.featured.fields["pin"] = FieldPreset(value=None, locked=True)
    with pytest.raises(ValueError, match="locked=true without a value"):
        _apply_featured_presets(record, {})


async def test_apply_presets_drops_non_manifest_fields(
    catalog: ComponentCatalog,
) -> None:
    """
    Optional fields not in the manifest are stripped from the output.

    The frontend's add-component form pre-fills every optional field
    with its catalog default; submitting that whole map shouldn't
    bloat the emitted YAML with values the manifest never asked for
    (a light's ``gamma_correct: 2.8``, an output's ``frequency: 1kHz``,
    ...). Required keys still ride through as a safety net.
    """
    record = catalog.get_featured_record("featured.apollo-esk-1.rgb_leds")
    assert record is not None
    out = _apply_featured_presets(
        record,
        {
            # Manifest keys — these stay.
            "pin": 14,
            "num_leds": 10,
            "rgb_order": "GRB",
            "name": "RGB LEDs",
            # Frontend default-fills the user never touched — these go.
            "gamma_correct": 2.8,
            "is_rgbw": False,
            "is_wrgb": False,
            "use_psram": True,
            "default_transition_length": "1s",
        },
    )
    assert "gamma_correct" not in out
    assert "is_rgbw" not in out
    assert "is_wrgb" not in out
    assert "use_psram" not in out
    assert "default_transition_length" not in out
    # Manifest-supplied chipset preset filled in even though the user
    # didn't submit it — manifest is authoritative.
    assert out["chipset"] == "WS2812"
    # Manifest fields the user did submit ride through unchanged.
    assert out["pin"] == 14
    assert out["num_leds"] == 10
    assert out["rgb_order"] == "GRB"


async def test_apply_presets_keeps_user_overridden_optional_field(
    catalog: ComponentCatalog,
) -> None:
    """
    A deliberate override of an optional field survives the filter.

    The frontend echoes the catalog default for fields the user
    didn't touch — those are stripped. But a value that differs from
    the default is real user intent and must ride through, even when
    the manifest doesn't curate the key.
    """
    record = catalog.get_featured_record("featured.apollo-esk-1.rgb_leds")
    assert record is not None
    out = _apply_featured_presets(
        record,
        {
            # Catalog default for ``gamma_correct`` is ``"2.8"`` —
            # 1.5 is a deliberate override and must be kept.
            "gamma_correct": 1.5,
            # ``is_rgbw`` defaults to False — flipping to True is an
            # override.
            "is_rgbw": True,
            # ``use_psram`` defaults to True — sending True is just an
            # echo and gets dropped.
            "use_psram": True,
        },
    )
    assert out["gamma_correct"] == 1.5
    assert out["is_rgbw"] is True
    assert "use_psram" not in out


async def test_apply_presets_strips_numeric_default_echo_across_types(
    catalog: ComponentCatalog,
) -> None:
    """
    Catalog stores numeric defaults as strings; a parsed-scalar echo still matches.

    ``gamma_correct`` is stored in ``components.json`` as the string
    ``"2.8"``. The frontend submits the parsed float ``2.8`` — the
    stringified compare bridges the two so the unmodified default is
    still recognised as noise.
    """
    record = catalog.get_featured_record("featured.apollo-esk-1.rgb_leds")
    assert record is not None
    out = _apply_featured_presets(record, {"gamma_correct": 2.8})
    assert "gamma_correct" not in out


async def test_apply_presets_keeps_required_field_outside_manifest(
    catalog: ComponentCatalog,
) -> None:
    """Required schema fields ride through even when the manifest omits them."""
    # Build a synthetic featured record whose manifest only sets ``id``,
    # then submit one of the underlying component's required fields and
    # confirm it survives the filter rather than being treated as
    # incidental frontend padding.
    record = deepcopy(catalog.get_featured_record("featured.apollo-esk-1.rgb_leds"))
    assert record is not None
    record.featured.fields = {"id": FieldPreset(value="rgb_leds")}
    out = _apply_featured_presets(record, {"pin": 14, "num_leds": 10, "rgb_order": "GRB"})
    # ``pin``, ``num_leds`` and ``rgb_order`` are schema-required — kept
    # despite being absent from the manifest.
    assert out["pin"] == 14
    assert out["num_leds"] == 10
    assert out["rgb_order"] == "GRB"


# ---------------------------------------------------------------------------
# YAML generation: top-level id auto-gen + nested entity sub-block autofill
# ---------------------------------------------------------------------------


async def test_generate_yaml_drops_dashed_id_via_empty_marker(
    catalog: ComponentCatalog,
) -> None:
    """An ``id: ""`` marker triggers the standard ``_generate_id`` auto-fill.

    ``add_component`` uses this for featured components so the frontend's
    dashed catalog-derived suggestion is replaced by a clean
    ``<unqualified>[_<name_slug>]``.
    """
    component = await catalog.get_component(component_id="switch.gpio")
    assert component is not None
    yaml = generate_component_yaml(component, {"pin": 12, "name": "Relay", "id": ""})
    assert "id: gpio_relay" in yaml
    assert "-" not in yaml.split("id: ")[1].splitlines()[0]


async def test_generate_yaml_autofills_subentity_name_and_id(
    catalog: ComponentCatalog,
) -> None:
    """Multi-sensor parents get ``name`` + ``id`` filled in on each reading.

    HLW8012-style components tag each reading with ``platform_type``; an
    empty ``current: {device_class: current}`` block must come back with
    a name and id or the sub-sensor won't surface in HA.
    """
    component = await catalog.get_component(component_id="sensor.hlw8012")
    assert component is not None
    yaml = generate_component_yaml(
        component,
        {
            "cf_pin": 3,
            "cf1_pin": 4,
            "sel_pin": 5,
            "model": "BL0937",
            "id": "",
            "current": {"device_class": "current", "unit_of_measurement": "A"},
            "energy": {"device_class": "energy"},
        },
    )
    # Top-level id auto-generated from the bare component stem.
    assert "id: hlw8012" in yaml
    # Sub-entities get a default ``name`` (from the entry label) and a
    # ``<parent_id>_<key>`` id, prepended ahead of user-supplied keys.
    assert "name: Current" in yaml
    assert "id: hlw8012_current" in yaml
    assert "name: Energy" in yaml
    assert "id: hlw8012_energy" in yaml


async def test_generate_yaml_preserves_user_supplied_subentity_name(
    catalog: ComponentCatalog,
) -> None:
    """The autofill only fills gaps — it never overwrites user input."""
    component = await catalog.get_component(component_id="sensor.hlw8012")
    assert component is not None
    yaml = generate_component_yaml(
        component,
        {
            "cf_pin": 3,
            "id": "plug",
            "current": {"name": "Plug Current", "id": "plug_amps"},
        },
    )
    assert "name: Plug Current" in yaml
    assert "id: plug_amps" in yaml
    # And the auto-id prefix tracks the user's chosen parent id.
    assert "id: plug" in yaml


async def test_generate_yaml_skips_autofill_for_non_entity_subblocks(
    catalog: ComponentCatalog,
) -> None:
    """Plain scalars / non-entity nested groups pass through untouched.

    Only entries with ``platform_type`` get the name/id autofill — a
    bare ``model: BL0937`` scalar must never grow a synthetic name.
    """
    component = await catalog.get_component(component_id="sensor.hlw8012")
    assert component is not None
    # A nested entry without platform_type should still emit verbatim.
    yaml = generate_component_yaml(
        component,
        {"cf_pin": 3, "id": "", "model": "BL0937"},
    )
    # ``model`` is a plain scalar — no name/id should attach to it.
    assert "name: Model" not in yaml
    assert "id: hlw8012_model" not in yaml


# ---------------------------------------------------------------------------
# add_component integration: featured-id reset + end-to-end YAML
# ---------------------------------------------------------------------------


def _make_controller(catalog: ComponentCatalog, tmp_path: Any) -> DevicesController:
    """Build a DevicesController with just enough plumbing for ``add_component``."""
    ctrl = DevicesController.__new__(DevicesController)
    ctrl._db = MagicMock()
    ctrl.state = DevicesState()
    ctrl._db.settings.rel_path = lambda name: tmp_path / name
    ctrl._db.components = catalog
    ctrl._scanner = MagicMock()
    ctrl._scanner.scan = AsyncMock()
    ctrl.state.esphome_cmd = []
    return ctrl


async def test_add_component_featured_resets_dashed_id(
    catalog: ComponentCatalog, tmp_path: Any
) -> None:
    """Frontend's dashed featured suggestion gets replaced by the standard auto-id."""
    (tmp_path / "plug.yaml").write_text("esphome:\n  name: plug\n", "utf-8")
    ctrl = _make_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="plug.yaml",
        component_id="featured.athom-smart-plug-v3.power_monitor",
        fields={
            # The frontend's catalog-derived id format is
            # ``featured_<board>_<local>_<n>``. The board portion
            # (``athom-smart-plug-v3``) carries dashes that the dashed-id
            # reset still has to detect and replace.
            "id": "featured_athom-smart-plug-v3_power_monitor_1",
            "current": {"device_class": "current"},
        },
    )

    # Auto-id from the manifest's ``fields.name: HLW8012 Power Monitor``;
    # ``_generate_id`` dedups the leading chip stem so we get
    # ``hlw8012_power_monitor`` rather than ``hlw8012_hlw8012_power_monitor``.
    assert "id: hlw8012_power_monitor" in response.yaml
    assert "featured_athom-smart-plug-v3" not in response.yaml
    assert "name: HLW8012 Power Monitor" in response.yaml
    # Sub-entity autofill rides through the merge step.
    assert "name: Current" in response.yaml
    assert "id: hlw8012_power_monitor_current" in response.yaml


async def test_add_component_featured_keeps_user_typed_id(
    catalog: ComponentCatalog, tmp_path: Any
) -> None:
    """A clean user-typed id (no dashes) survives the featured id-reset."""
    (tmp_path / "plug.yaml").write_text("esphome:\n  name: plug\n", "utf-8")
    ctrl = _make_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="plug.yaml",
        component_id="featured.sonoff-basic.relay",
        fields={"pin": 12, "name": "Relay", "id": "main_relay"},
    )

    assert "id: main_relay" in response.yaml


async def test_add_component_featured_unknown_id_raises(
    catalog: ComponentCatalog, tmp_path: Any
) -> None:
    """An unknown ``featured.*`` id surfaces as a clear ValueError."""
    ctrl = _make_controller(catalog, tmp_path)

    with pytest.raises(ValueError, match="Unknown featured component"):
        await ctrl.add_component(
            configuration="plug.yaml",
            component_id="featured.no-such-board.x",
            fields={},
        )


async def test_add_component_featured_emits_explicit_name_and_id(
    catalog: ComponentCatalog, tmp_path: Any
) -> None:
    """
    Regression: featured ``binary_sensor.gpio`` entries emit ``name:`` and ``id:``.

    The Sonoff Basic's "Front-Panel Button" used to emit a YAML block
    with neither ``name:`` nor ``id:`` — the resulting entity stayed
    unnamed in Home Assistant. Now the manifest carries explicit
    ``fields.id`` / ``fields.name`` presets so the YAML always lands
    with both, no runtime auto-derivation needed.
    """
    (tmp_path / "sonoff.yaml").write_text("esphome:\n  name: sonoff\n", "utf-8")
    ctrl = _make_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="sonoff.yaml",
        component_id="featured.sonoff-basic.button",
        fields={},
    )

    assert "binary_sensor:" in response.yaml
    assert "platform: gpio" in response.yaml
    assert "name: Front-Panel Button" in response.yaml
    assert "id: button" in response.yaml


async def test_add_component_featured_non_entity_emits_id_only(
    catalog: ComponentCatalog, tmp_path: Any
) -> None:
    """
    Non-entity featured components (``output:``, ``i2c:``, ...) get only ``id:``.

    Their manifest entries carry ``fields.id`` but no ``fields.name`` —
    ESPHome's ``output:`` schema doesn't accept a top-level ``name:``,
    and the manifest is the only source for what fields land in the YAML.
    """
    (tmp_path / "sonoff.yaml").write_text("esphome:\n  name: sonoff\n", "utf-8")
    ctrl = _make_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="sonoff.yaml",
        component_id="featured.sonoff-basic.status_led_output",
        fields={},
    )

    assert "output:" in response.yaml
    assert "id: status_led_output" in response.yaml
    assert "name:" not in response.yaml.split("output:")[1]


async def test_add_component_featured_drops_non_manifest_defaults(
    catalog: ComponentCatalog, tmp_path: Any
) -> None:
    """
    The emitted YAML for a featured component carries only manifest fields.

    End-to-end check that bridges the two halves of the fix: the
    frontend can submit a full form's worth of catalog defaults, and
    the YAML still comes out as the curated short block the manifest
    describes — no ``gamma_correct``/``is_rgbw``/``use_psram`` noise.
    """
    (tmp_path / "kit.yaml").write_text("esphome:\n  name: kit\napi:\n", "utf-8")
    ctrl = _make_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="kit.yaml",
        component_id="featured.apollo-esk-1.rgb_leds",
        fields={
            # Frontend default-fills:
            "gamma_correct": 2.8,
            "is_rgbw": False,
            "is_wrgb": False,
            "use_psram": True,
            "default_transition_length": "1s",
            "flash_transition_length": "0s",
            "reset_high": "0 us",
            "reset_low": "0 us",
            "restore_mode": "ALWAYS_OFF",
        },
    )

    yaml = response.yaml
    assert "platform: esp32_rmt_led_strip" in yaml
    # Manifest-curated fields land in the YAML.
    assert "chipset: WS2812" in yaml
    assert "num_leds: 10" in yaml
    assert "rgb_order: GRB" in yaml
    # Frontend's default-fills are stripped.
    for noise in (
        "gamma_correct",
        "is_rgbw",
        "is_wrgb",
        "use_psram",
        "default_transition_length",
        "flash_transition_length",
        "reset_high",
        "reset_low",
        "restore_mode",
    ):
        assert noise not in yaml, f"{noise} should have been filtered out"


async def test_add_component_strips_mqtt_fields_when_no_mqtt_block(
    catalog: ComponentCatalog, tmp_path: Any
) -> None:
    """
    Drop MQTT-only fields when the device YAML has no ``mqtt:`` block.

    Featured components target the dashboard's native-API setup, but the
    frontend can still pass MQTT defaults through (form auto-fill,
    programmatic adds). Each field's ``depends_on_component`` gate is
    honoured against the device YAML so the resulting block stays clean.
    """
    (tmp_path / "plug.yaml").write_text("esphome:\n  name: plug\napi:\n", "utf-8")
    ctrl = _make_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="plug.yaml",
        component_id="featured.athom-smart-plug-v3.relay",
        fields={
            "availability": {"payload_available": "online"},
            "qos": "0",
            "discovery": True,
        },
    )

    assert "availability" not in response.yaml
    assert "qos" not in response.yaml
    assert "discovery" not in response.yaml


async def test_add_component_keeps_mqtt_fields_when_mqtt_block_present(
    catalog: ComponentCatalog, tmp_path: Any
) -> None:
    """When the device already has an ``mqtt:`` block, MQTT fields ride through."""
    existing = "esphome:\n  name: plug\napi:\nmqtt:\n  broker: mqtt.local\n"
    (tmp_path / "plug.yaml").write_text(existing, "utf-8")
    ctrl = _make_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="plug.yaml",
        component_id="featured.athom-smart-plug-v3.relay",
        fields={"availability": {"payload_available": "online"}},
    )

    assert "availability:" in response.yaml
    assert "payload_available: online" in response.yaml


async def test_add_component_handles_secret_tags_in_existing_yaml(
    catalog: ComponentCatalog, tmp_path: Any
) -> None:
    """
    ESPHome ``!secret`` / ``!include`` tags don't disable the dependency gate.

    A regex-based top-level scan is what makes this work — ``yaml.safe_load``
    would raise on the unknown ``!secret`` tag and silently no-op the filter,
    leaking MQTT defaults back into the YAML on most real-world configs.
    """
    existing = (
        "esphome:\n  name: plug\napi:\n"
        "mqtt:\n  broker: !secret mqtt_broker\n  username: !secret mqtt_user\n"
    )
    (tmp_path / "plug.yaml").write_text(existing, "utf-8")
    ctrl = _make_controller(catalog, tmp_path)

    response = await ctrl.add_component(
        configuration="plug.yaml",
        component_id="featured.athom-smart-plug-v3.relay",
        fields={"availability": {"payload_available": "online"}},
    )

    # mqtt block IS present (just uses !secret) — so MQTT fields stay.
    assert "availability:" in response.yaml
    assert "payload_available: online" in response.yaml


async def test_add_component_schedules_storage_regenerate(
    catalog: ComponentCatalog, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``add_component`` schedules a StorageJSON regen after the write."""
    (tmp_path / "plug.yaml").write_text("esphome:\n  name: plug\n", "utf-8")
    ctrl = _make_controller(catalog, tmp_path)
    scheduled: list[str] = []
    monkeypatch.setattr(ctrl, "_schedule_storage_regenerate", scheduled.append, raising=False)

    await ctrl.add_component(
        configuration="plug.yaml",
        component_id="featured.athom-smart-plug-v3.relay",
        fields={},
    )

    assert scheduled == ["plug.yaml"]


def test_drop_unconfigured_dependent_fields_recurses_into_nested_dicts() -> None:
    """Nested dict values get filtered against their sub-entries' dependencies."""
    # Synthetic schema: a nested ``readings`` block whose sub-fields
    # gate on different top-level dependencies. SimpleNamespace mirrors
    # the duck-typed access ``ConfigEntry`` provides without dragging in
    # every required field of the dataclass.
    component = SimpleNamespace(
        id="sensor.synthetic",
        config_entries=[
            SimpleNamespace(
                key="readings",
                depends_on_component=None,
                config_entries=[
                    SimpleNamespace(
                        key="availability",
                        depends_on_component="mqtt",
                        config_entries=None,
                    ),
                    SimpleNamespace(
                        key="web_url",
                        depends_on_component="web_server",
                        config_entries=None,
                    ),
                    SimpleNamespace(
                        key="threshold",
                        depends_on_component=None,
                        config_entries=None,
                    ),
                ],
            )
        ],
    )

    fields = {
        "readings": {
            "availability": "online",
            "web_url": "/foo",
            "threshold": 42,
        }
    }
    out = _drop_unconfigured_dependent_fields(fields, component, "esphome:\n  name: x\n")
    # Both gated sub-fields gone, the unconditional one stays.
    assert out == {"readings": {"threshold": 42}}


def test_drop_unconfigured_dependent_fields_keeps_self_domain() -> None:
    """Adding the gating component itself counts the new domain as configured."""
    # ``mqtt.discovery`` itself carries ``depends_on_component: mqtt`` —
    # without the self-domain allowance, adding ``mqtt:`` for the first
    # time would silently drop ``discovery`` from the user's payload.
    mqtt = SimpleNamespace(
        id="mqtt",
        config_entries=[
            SimpleNamespace(key="broker", depends_on_component=None, config_entries=None),
            SimpleNamespace(key="discovery", depends_on_component="mqtt", config_entries=None),
        ],
    )
    out = _drop_unconfigured_dependent_fields(
        {"broker": "host", "discovery": True},
        mqtt,
        "esphome:\n  name: x\napi:\n",
    )
    assert out == {"broker": "host", "discovery": True}
