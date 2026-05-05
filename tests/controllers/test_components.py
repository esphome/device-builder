"""Targeted unit tests filling coverage gaps in ``components.py``.

The other ``test_components_*`` files in ``tests/`` cover the
catalog's happy-path shape end-to-end against the real shipped
JSON. This file pins the small edge-case branches the wider
suite doesn't reach:

- ``load()`` empty-when-missing path.
- ``get_integration_docs`` skip-when-no-id-or-docs.
- ``get_component`` returns ``None`` for an unknown id.
- ``get_components`` ``exclude_category`` + ``query`` filters.
- ``_build_featured_registry`` empty-when-no-boards and
  warn-on-unknown-component-id branches.
- ``_featured_components_for_board`` skips when the index
  diverges from ``_featured_by_id``.
- ``_resolve_platform`` lower-casing + unknown-board-id branches.
- ``_load_pin_features`` / ``_load_options`` rejection paths.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers.boards import BoardCatalog
from esphome_device_builder.controllers.components import (
    INTERNAL_COMPONENT_IDS,
    ComponentCatalog,
    _FeaturedRecord,
    _load_options,
    _load_pin_features,
)
from esphome_device_builder.models import (
    ComponentCatalogEntry,
    ComponentCategory,
    FeaturedComponent,
    PinFeature,
)


class _Container:
    """Minimal ``device_builder``-shaped object the catalog reads from."""

    def __init__(self, boards: BoardCatalog | None = None) -> None:
        self.boards = boards
        self.components: ComponentCatalog | None = None


def _make_entry(
    *,
    entry_id: str,
    name: str = "",
    description: str = "",
    category: ComponentCategory = ComponentCategory.MISC,
    docs_url: str = "",
    supported_platforms: list[str] | None = None,
) -> ComponentCatalogEntry:
    """Build a minimal ``ComponentCatalogEntry`` for catalog-state tests.

    Real catalog entries have ~20 fields; the dataclass defaults
    cover the ones we don't care about for these branch tests.
    """
    return ComponentCatalogEntry(
        id=entry_id,
        name=name or entry_id,
        description=description,
        category=category,
        docs_url=docs_url,
        image_url="",
        dependencies=[],
        multi_conf=False,
        supported_platforms=supported_platforms or [],
        config_entries=[],
    )


# ── load() ──────────────────────────────────────────────────────────


def test_load_warns_and_leaves_catalog_empty_when_components_json_missing(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Missing ``components.json`` warns and leaves the catalog empty.

    The catalog should not crash when the JSON is absent — it
    logs a warning and stays empty so the rest of the controller
    can run (with empty results) until the file is regenerated.
    """
    missing = tmp_path / "no-such-components.json"
    cat = ComponentCatalog()
    with (
        patch(
            "esphome_device_builder.controllers.components._COMPONENTS_JSON",
            missing,
        ),
        caplog.at_level(logging.WARNING),
    ):
        cat.load()
    assert cat._components == []
    assert cat._by_id == {}
    assert any("Component catalog not found" in rec.message for rec in caplog.records)


def test_load_filters_out_internal_helper_components(tmp_path: Path) -> None:
    """Every id in ``INTERNAL_COMPONENT_IDS`` is dropped at load time.

    These are ESPHome internal helpers auto-loaded by their public-
    facing parent (e.g. ``web_server`` pulls in ``web_server_base``
    / ``web_server_idf``). Surfacing them in the Add Configuration
    picker is just noise — issue #325. The denylist drives the
    filter, so the test loops over the live constant rather than
    hard-coding the two current entries; that keeps this test honest
    when the denylist is extended (and catches a regression that
    drops the filter against the same set of inputs).
    """
    user_facing = {
        "id": "web_server",
        "name": "Web Server",
        "category": "core",
        "config_entries": [],
    }
    components = [user_facing] + [
        {"id": cid, "name": cid, "category": "core", "config_entries": []}
        for cid in INTERNAL_COMPONENT_IDS
    ]
    components_json = tmp_path / "components.json"
    components_json.write_text(json.dumps({"components": components}))

    cat = ComponentCatalog()
    with patch(
        "esphome_device_builder.controllers.components._COMPONENTS_JSON",
        components_json,
    ):
        cat.load()

    ids = {c.id for c in cat._components}
    assert "web_server" in ids, "user-facing web_server entry must survive"
    for cid in INTERNAL_COMPONENT_IDS:
        assert cid not in ids, f"{cid} must be filtered out at load time"
        assert cat._by_id.get(cid) is None


def test_internal_component_ids_is_single_source_of_truth() -> None:
    """The sync script imports the runtime denylist — one set, not two.

    Previously the constant was duplicated between the runtime
    catalog loader and the build-time JSON generator, and a future
    contributor could update one and forget the other. The
    contract now is: ``script/sync_components.py`` imports
    ``INTERNAL_COMPONENT_IDS`` from
    ``esphome_device_builder.controllers.components``. Pin that
    invariant so a regression that re-duplicates the set or
    diverges the values is caught here. Imported lazily because
    the sync module is a script and pulls in heavier
    dependencies; the import here is the assertion.
    """
    from script.sync_components import (  # noqa: PLC0415 — see docstring
        _INTERNAL_COMPONENT_IDS as SYNC_INTERNAL_IDS,
    )

    assert SYNC_INTERNAL_IDS is INTERNAL_COMPONENT_IDS


# ── get_integration_docs() ──────────────────────────────────────────


async def test_get_integration_docs_skips_entries_with_no_id_or_no_docs() -> None:
    """Entries with empty ``id`` or ``docs_url`` contribute nothing.

    Real catalog entries always have both, but the loop guards
    against bad data so a malformed sync doesn't poison the map.
    """
    cat = ComponentCatalog()
    cat._components = [
        _make_entry(entry_id="wifi", docs_url="https://esphome.io/components/wifi"),
        _make_entry(entry_id="", docs_url="https://example/empty-id"),
        _make_entry(entry_id="no_docs", docs_url=""),
    ]
    docs = await cat.get_integration_docs()
    assert "wifi" in docs
    # Empty id never lands in the output — the dict can't key on "".
    assert "" not in docs
    # Empty docs_url stays out too.
    assert "no_docs" not in docs


# ── get_component() ─────────────────────────────────────────────────


async def test_get_component_returns_none_for_unknown_id() -> None:
    """Unknown ``component_id`` resolves to ``None`` rather than a crash."""
    cat = ComponentCatalog()
    cat._components = [_make_entry(entry_id="wifi")]
    cat._by_id = {"wifi": cat._components[0]}
    assert await cat.get_component(component_id="does-not-exist") is None


# ── get_components() ────────────────────────────────────────────────


async def test_get_components_exclude_category_drops_matching_entries() -> None:
    """``exclude_category`` is the inverse filter the regular component selector uses.

    The dashboard's "Add core configuration" dialog has its own
    list; the regular component selector hides the ``core``
    umbrella entries via this filter.
    """
    cat = ComponentCatalog()
    cat._components = [
        _make_entry(entry_id="wifi", category=ComponentCategory.CORE),
        _make_entry(entry_id="dht", category=ComponentCategory.SENSOR),
        _make_entry(entry_id="gpio", category=ComponentCategory.SWITCH),
    ]
    cat._by_id = {c.id: c for c in cat._components}
    res = await cat.get_components(exclude_category=ComponentCategory.CORE.value)
    ids = {c.id for c in res.components}
    assert "wifi" not in ids
    assert {"dht", "gpio"} <= ids


async def test_get_components_query_matches_name_description_or_id() -> None:
    """``query`` is a substring match against name / description / id."""
    cat = ComponentCatalog()
    cat._components = [
        _make_entry(entry_id="wifi", name="Wi-Fi", description="Wireless networking"),
        _make_entry(entry_id="ethernet", name="Ethernet", description="Wired networking"),
        _make_entry(entry_id="dht", name="DHT", description="Temperature sensor"),
    ]
    cat._by_id = {c.id: c for c in cat._components}
    # ``networking`` matches description on wifi + ethernet.
    res = await cat.get_components(query="networking")
    ids = {c.id for c in res.components}
    assert ids == {"wifi", "ethernet"}
    # Match by id stem too.
    res = await cat.get_components(query="dht")
    assert {c.id for c in res.components} == {"dht"}


# ── _build_featured_registry() ──────────────────────────────────────


def test_build_featured_registry_is_empty_when_no_boards() -> None:
    """No boards → empty featured registry, no crash.

    The featured registry depends on the boards catalog; a
    catalog constructed without one (or whose ``boards`` is
    ``None``) builds an empty registry rather than crashing.
    """
    cat = ComponentCatalog(_Container(boards=None))
    cat._build_featured_registry()
    assert cat._featured_by_id == {}
    assert cat._featured_by_board == {}


def test_build_featured_registry_skips_and_warns_on_unknown_component_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown ``component_id`` in a featured ref logs and skips.

    A board can declare a featured component whose
    ``component_id`` doesn't resolve in the catalog (bad
    reference, stale board JSON). The registry should log a
    warning and skip rather than poison the index with a
    half-built record.
    """
    boards_cat = BoardCatalog()
    boards_cat.load()
    cat = ComponentCatalog(_Container(boards=boards_cat))
    # Pick any real board and inject a featured-component referencing
    # a deliberately-unknown component_id. The warning path runs
    # purely against ``self._by_id`` so we don't need a fully-loaded
    # component catalog — just one that doesn't have the bogus id.
    target_board = boards_cat.iter_boards()[0]
    target_board.featured_components.append(
        FeaturedComponent(id="zzz_test_phantom", component_id="not.a.real.component")
    )
    try:
        with caplog.at_level(logging.WARNING):
            cat._build_featured_registry()
        assert any("references unknown component" in rec.message for rec in caplog.records)
        # The phantom id never lands in the index.
        assert not any(full_id.endswith(".zzz_test_phantom") for full_id in cat._featured_by_id)
    finally:
        # Restore the shared board so other tests aren't polluted.
        target_board.featured_components.pop()


# ── _featured_components_for_board() ────────────────────────────────


def test_featured_components_for_board_skips_records_missing_from_index() -> None:
    """Skips when ``_featured_by_board`` and ``_featured_by_id`` diverge.

    The two indexes are populated together in
    ``_build_featured_registry``, but the skip lets a
    rebuild-mid-flight recover gracefully.
    """
    cat = ComponentCatalog()
    cat._featured_by_board = {"phantom-board": ["featured.phantom-board.ghost"]}
    cat._featured_by_id = {}  # diverged: id missing
    out = cat._featured_components_for_board("phantom-board", target_platform=None, query=None)
    assert out == []


# ── _resolve_platform() ─────────────────────────────────────────────


def test_resolve_platform_lowers_explicit_platform_arg() -> None:
    """Explicit ``platform`` arg is lower-cased for catalog matching.

    Frontend-supplied platforms come through verbatim
    (``"ESP32"``); the catalog's ``supported_platforms`` are
    lower-case, so resolve lower-cases for matching.
    """
    cat = ComponentCatalog()
    assert cat._resolve_platform("ESP32", board_id=None) == "esp32"
    assert cat._resolve_platform("Esp8266", board_id=None) == "esp8266"


def test_resolve_platform_returns_none_for_unknown_board_id() -> None:
    """Unknown ``board_id`` resolves to ``None`` rather than throwing.

    The catalog stays usable (every entry is treated as
    platform-agnostic) while the bad input gets logged
    elsewhere.
    """
    boards_cat = BoardCatalog()
    cat = ComponentCatalog(_Container(boards=boards_cat))
    assert cat._resolve_platform(None, board_id="no-such-board-zzz") is None


# ── _load_pin_features() / _load_options() ──────────────────────────


def test_load_pin_features_drops_unknown_values() -> None:
    """Unknown pin-feature strings are silently dropped.

    Known features pass through. The catalog's ``pin_features``
    is expected to be a list of valid enum values, but a
    sync-script bug or schema drift shouldn't crash the load.
    """
    out = _load_pin_features([PinFeature.ADC.value, "definitely-not-a-feature"])
    assert PinFeature.ADC in out
    assert len(out) == 1


def test_load_options_accepts_plain_string_list() -> None:
    """Plain-string options list round-trips into ConfigValueOption pairs.

    Each string is used as both label and value. The dict-shaped
    form is handled by the next branch.
    """
    out = _load_options(["yes", "no"])
    assert out is not None
    assert len(out) == 2
    assert out[0].label == "yes"
    assert out[0].value == "yes"
    assert out[1].label == "no"
    assert out[1].value == "no"


def test_featured_record_underlying_id_returns_full_underlying_id() -> None:
    """``_FeaturedRecord.underlying_id`` delegates to the catalog entry's id."""
    record = _FeaturedRecord(
        full_id="featured.example.relay",
        board_id="example",
        featured=FeaturedComponent(id="relay", component_id="switch.gpio"),
        underlying=_make_entry(entry_id="switch.gpio", category=ComponentCategory.SWITCH),
    )
    assert record.underlying_id == "switch.gpio"
