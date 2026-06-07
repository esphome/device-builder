"""Targeted unit tests filling coverage gaps in ``components.py``.

The other ``test_components_*`` files in ``tests/`` cover the
catalog's happy-path shape end-to-end against the real shipped
JSON. This file pins the small edge-case branches the wider
suite doesn't reach:

- ``load()`` empty-when-missing path.
- ``get_integration_docs`` skip-when-no-id-or-docs.
- ``get_component_bodies`` returns an empty dict for an unknown id.
- ``get_components`` ``exclude_category`` + ``query`` filters.
- ``_build_featured_registry`` empty-when-no-boards and
  warn-on-unknown-component-id branches.
- ``_featured_components_for_board`` skips when the index
  diverges from ``_featured_by_id``.
- ``_resolve_platform`` lower-casing + unknown-board-id branches.
"""

from __future__ import annotations

import asyncio
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
)
from esphome_device_builder.controllers.components import _resolve as components_module
from esphome_device_builder.models import (
    ComponentCatalogIndexEntry,
    ComponentCategory,
    ConfigEntry,
    ConfigEntryType,
    FeaturedComponent,
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
    provides: list[str] | None = None,
) -> ComponentCatalogIndexEntry:
    """Build a minimal ``ComponentCatalogIndexEntry`` for catalog-state tests.

    The catalog now holds slim index entries in memory; bodies
    hydrate lazily through ``get_body``. Tests that just need an
    entry to live in ``_components`` / ``_by_id`` use this helper.
    """
    return ComponentCatalogIndexEntry(
        id=entry_id,
        name=name or entry_id,
        description=description,
        category=category,
        docs_url=docs_url,
        image_url="",
        dependencies=[],
        multi_conf=False,
        supported_platforms=supported_platforms or [],
        provides=provides or [],
    )


# ── load() ──────────────────────────────────────────────────────────


def test_load_warns_and_leaves_catalog_empty_when_index_missing(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Missing ``components.index.json`` warns and leaves the catalog empty.

    The catalog should not crash when the index is absent — it
    logs a warning and stays empty so the rest of the controller
    can run (with empty results) until the file is regenerated.
    """
    missing = tmp_path / "no-such-components.index.json"
    cat = ComponentCatalog()
    with (
        patch(
            "esphome_device_builder.controllers.components.controller._COMPONENTS_INDEX_JSON",
            missing,
        ),
        caplog.at_level(logging.WARNING),
    ):
        cat.load()
    assert cat._components == []
    assert cat._by_id == {}
    assert any("Component index not found" in rec.message for rec in caplog.records)


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
    user_facing = {"id": "web_server", "name": "Web Server", "category": "core", "description": ""}
    components = [user_facing] + [
        {"id": cid, "name": cid, "category": "core", "description": ""}
        for cid in INTERNAL_COMPONENT_IDS
    ]
    index_path = tmp_path / "components.index.json"
    index_path.write_text(json.dumps({"components": components}))

    cat = ComponentCatalog()
    with patch(
        "esphome_device_builder.controllers.components.controller._COMPONENTS_INDEX_JSON",
        index_path,
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


# ── get_component_bodies() unknown-id branch ────────────────────────


async def test_get_component_bodies_omits_unknown_ids() -> None:
    """Unknown ``component_id`` is silently dropped from the response."""
    cat = ComponentCatalog()
    cat._components = [_make_entry(entry_id="wifi")]
    cat._by_id = {"wifi": cat._components[0]}
    assert await cat.get_component_bodies(component_ids=["does-not-exist"]) == {}


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


async def test_get_components_provides_filters_to_interface_providers() -> None:
    """``provides`` returns only components advertising the requested interface.

    Cross-domain reference fields (ct_clamp's ``sensor`` →
    ``voltage_sampler``) drive the Add-component picker through this
    filter, since the providers live under ``sensor:``, not a
    ``voltage_sampler:`` block.
    """
    cat = ComponentCatalog()
    cat._components = [
        _make_entry(
            entry_id="sensor.adc", category=ComponentCategory.SENSOR, provides=["voltage_sampler"]
        ),
        _make_entry(
            entry_id="sensor.ads1115",
            category=ComponentCategory.SENSOR,
            provides=["voltage_sampler"],
        ),
        _make_entry(entry_id="sensor.dht", category=ComponentCategory.SENSOR),
    ]
    cat._by_id = {c.id: c for c in cat._components}
    res = await cat.get_components(provides="voltage_sampler")
    assert {c.id for c in res.components} == {"sensor.adc", "sensor.ads1115"}
    # Combines with the category filter rather than overriding it.
    res = await cat.get_components(
        provides="voltage_sampler", category=ComponentCategory.SWITCH.value
    )
    assert res.components == []


async def test_get_components_provides_filters_featured_entries() -> None:
    """``provides`` narrows featured entries too, keeping both result sets consistent.

    A ``category=featured`` query carrying ``provides`` must drop a
    featured card whose underlying component doesn't advertise the
    interface, not pass it through unfiltered.
    """
    cat = ComponentCatalog()
    cat._by_id = {
        "sensor.adc": _make_entry(
            entry_id="sensor.adc", category=ComponentCategory.SENSOR, provides=["voltage_sampler"]
        ),
        "sensor.dht": _make_entry(entry_id="sensor.dht", category=ComponentCategory.SENSOR),
    }
    cat._featured_by_id = {
        "featured.b.adc": _FeaturedRecord(
            full_id="featured.b.adc",
            board_id="b",
            featured=FeaturedComponent(id="adc", component_id="sensor.adc"),
            underlying_id="sensor.adc",
        ),
        "featured.b.dht": _FeaturedRecord(
            full_id="featured.b.dht",
            board_id="b",
            featured=FeaturedComponent(id="dht", component_id="sensor.dht"),
            underlying_id="sensor.dht",
        ),
    }
    cat._featured_by_board = {"b": ["featured.b.adc", "featured.b.dht"]}
    res = await cat.get_components(
        category=ComponentCategory.FEATURED.value, board_id="b", provides="voltage_sampler"
    )
    assert {c.id for c in res.components} == {"featured.b.adc"}


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


async def test_get_components_response_categories_track_query_filter() -> None:
    """
    Response ``categories`` reflect the active ``query``.

    Buckets with no post-filter matches drop out entirely so the
    frontend can hide empty categories.
    """
    cat = ComponentCatalog()
    cat._components = [
        _make_entry(entry_id="dht", name="DHT", category=ComponentCategory.SENSOR),
        _make_entry(entry_id="bme280", name="BME280", category=ComponentCategory.SENSOR),
        _make_entry(entry_id="debug", name="Debug", category=ComponentCategory.CORE),
        _make_entry(entry_id="gpio", name="GPIO", category=ComponentCategory.SWITCH),
    ]
    cat._by_id = {c.id: c for c in cat._components}

    res = await cat.get_components(query="debug")
    counts = {c["id"]: c["count"] for c in res.categories}
    # The non-matching buckets are absent from the response (not zero).
    assert counts == {ComponentCategory.CORE.value: 1}


async def test_get_components_response_categories_ignore_selected_category() -> None:
    """
    A selected ``category`` doesn't shrink the sidebar.

    The user needs every category visible to navigate between
    them; only query / exclude / platform narrow the bucket list.
    """
    cat = ComponentCatalog()
    cat._components = [
        _make_entry(entry_id="dht", category=ComponentCategory.SENSOR),
        _make_entry(entry_id="gpio", category=ComponentCategory.SWITCH),
    ]
    cat._by_id = {c.id: c for c in cat._components}

    res = await cat.get_components(category=ComponentCategory.SENSOR.value)
    ids = {c["id"] for c in res.categories}
    assert ids == {ComponentCategory.SENSOR.value, ComponentCategory.SWITCH.value}


async def test_get_components_response_categories_honor_exclude_and_platform() -> None:
    """``exclude_category`` and ``platform`` filters drop matching buckets too."""
    cat = ComponentCatalog()
    cat._components = [
        _make_entry(entry_id="wifi", category=ComponentCategory.CORE),
        _make_entry(
            entry_id="esp32-only",
            category=ComponentCategory.SENSOR,
            supported_platforms=["esp32"],
        ),
        _make_entry(
            entry_id="esp8266-only",
            category=ComponentCategory.SWITCH,
            supported_platforms=["esp8266"],
        ),
    ]
    cat._by_id = {c.id: c for c in cat._components}

    res = await cat.get_components(exclude_category=ComponentCategory.CORE.value)
    assert all(c["id"] != ComponentCategory.CORE.value for c in res.categories)

    # ``esp8266-only`` is platform-incompatible and shouldn't contribute.
    res = await cat.get_components(platform="esp32")
    counts = {c["id"]: c["count"] for c in res.categories}
    assert counts == {
        ComponentCategory.CORE.value: 1,
        ComponentCategory.SENSOR.value: 1,
    }


async def test_get_categories_endpoint_unaffected_by_query_filter_change() -> None:
    """
    The standalone ``get_categories`` endpoint stays unfiltered.

    Only ``get_components`` shares its request filters with the
    counter; ``get_categories`` always returns the full breakdown.
    """
    cat = ComponentCatalog()
    cat._components = [
        _make_entry(entry_id="dht", category=ComponentCategory.SENSOR),
        _make_entry(entry_id="gpio", category=ComponentCategory.SWITCH),
        _make_entry(entry_id="wifi", category=ComponentCategory.CORE),
    ]
    cat._by_id = {c.id: c for c in cat._components}

    cats = await cat.get_categories()
    counts = {c["id"]: c["count"] for c in cats}
    assert counts == {
        ComponentCategory.SENSOR.value: 1,
        ComponentCategory.SWITCH.value: 1,
        ComponentCategory.CORE.value: 1,
    }


# ── _build_featured_registry() ──────────────────────────────────────


def test_build_featured_registry_is_empty_when_index_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty ``featured_components.index.json`` builds an empty registry.

    Post-split, the registry reads from the precomputed index
    rather than walking board bodies; the no-boards-loaded case
    becomes "the index is empty," which still has to short-circuit
    cleanly without crashing.
    """
    monkeypatch.setattr(
        "esphome_device_builder.controllers.components.controller.load_featured_components_index",
        dict,
    )
    cat = ComponentCatalog(_Container(boards=None))
    cat._build_featured_registry()
    assert cat._featured_by_id == {}
    assert cat._featured_by_board == {}


def test_build_featured_registry_skips_and_warns_on_unknown_component_id(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown ``component_id`` in a featured ref logs and skips.

    A board can declare a featured component whose ``component_id``
    doesn't resolve in the catalog (bad reference, stale board
    JSON). The registry should log a warning and skip rather than
    poison the index with a half-built record.

    Post-split, featured components come from
    ``featured_components.index.json`` rather than board bodies, so
    monkeypatch the loader directly to inject the phantom.
    """
    cat = ComponentCatalog(_Container(boards=None))
    phantom = FeaturedComponent(id="zzz_test_phantom", component_id="not.a.real.component")
    monkeypatch.setattr(
        "esphome_device_builder.controllers.components.controller.load_featured_components_index",
        lambda: {"some_board": [phantom]},
    )
    with caplog.at_level(logging.WARNING):
        cat._build_featured_registry()
    assert any("references unknown component" in rec.message for rec in caplog.records)
    assert not any(full_id.endswith(".zzz_test_phantom") for full_id in cat._featured_by_id)


# ── _featured_components_for_board() ────────────────────────────────


def test_featured_components_for_board_skips_underlying_missing_from_index() -> None:
    """A featured record whose underlying id vanished from the slim index is skipped.

    Defensive branch in ``_featured_components_for_board``: the
    featured registry survives an entry being dropped from the
    main index (sync regen mid-flight, hand-edited override).
    The skim listing must drop the orphan rather than reach into
    ``None``.
    """
    cat = ComponentCatalog()
    cat._by_id = {}  # underlying "switch.gpio" deliberately not present
    cat._featured_by_id = {
        "featured.bench-board.relay": _FeaturedRecord(
            full_id="featured.bench-board.relay",
            board_id="bench-board",
            featured=FeaturedComponent(id="relay", component_id="switch.gpio"),
            underlying_id="switch.gpio",
        )
    }
    cat._featured_by_board = {"bench-board": ["featured.bench-board.relay"]}

    entries = cat._featured_components_for_board("bench-board", query=None)

    assert entries == []


def test_featured_components_for_board_skips_records_missing_from_index() -> None:
    """Skips when ``_featured_by_board`` and ``_featured_by_id`` diverge.

    The two indexes are populated together in
    ``_build_featured_registry``, but the skip lets a
    rebuild-mid-flight recover gracefully.
    """
    cat = ComponentCatalog()
    cat._featured_by_board = {"phantom-board": ["featured.phantom-board.ghost"]}
    cat._featured_by_id = {}  # diverged: id missing
    out = cat._featured_components_for_board("phantom-board", query=None)
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


def test_featured_record_carries_underlying_id() -> None:
    """``_FeaturedRecord.underlying_id`` is the catalog id the body lookups go through."""
    record = _FeaturedRecord(
        full_id="featured.example.relay",
        board_id="example",
        featured=FeaturedComponent(id="relay", component_id="switch.gpio"),
        underlying_id="switch.gpio",
    )
    assert record.underlying_id == "switch.gpio"


# ── get_body() ──────────────────────────────────────────────────────


async def test_get_body_returns_none_for_id_absent_from_index(tmp_path: Path) -> None:
    """Unknown ids short-circuit before touching disk."""
    cat = ComponentCatalog()
    cat._by_id = {"wifi": _make_entry(entry_id="wifi")}
    assert await cat.get_body("does-not-exist") is None


async def test_get_body_reads_from_disk_and_caches(tmp_path: Path) -> None:
    """First call hydrates from disk; second call hits the LRU."""
    cat = ComponentCatalog()
    cat._by_id = {"wifi": _make_entry(entry_id="wifi")}
    bodies_dir = tmp_path / "components"
    bodies_dir.mkdir()
    (bodies_dir / "wifi.json").write_text(
        json.dumps(
            {
                "id": "wifi",
                "name": "Wi-Fi",
                "category": "core",
                "description": "",
                "config_entries": [],
            }
        )
    )
    with patch(
        "esphome_device_builder.controllers.components._resolve._COMPONENT_BODIES_DIR",
        bodies_dir,
    ):
        first = await cat.get_body("wifi")
        second = await cat.get_body("wifi")

    assert first is not None
    assert first.id == "wifi"
    # The second call must hit the cache — exposing the identity here pins
    # the LRU contract; if hydrate-on-every-call regresses, this fails.
    assert second is first


async def test_get_body_evicts_least_recently_used(tmp_path: Path) -> None:
    """LRU stays bounded under repeated detail-view opens."""
    cat = ComponentCatalog()
    cat._by_id = {f"comp_{i}": _make_entry(entry_id=f"comp_{i}") for i in range(70)}
    bodies_dir = tmp_path / "components"
    bodies_dir.mkdir()
    for i in range(70):
        (bodies_dir / f"comp_{i}.json").write_text(
            json.dumps(
                {
                    "id": f"comp_{i}",
                    "name": f"comp_{i}",
                    "category": "misc",
                    "description": "",
                    "config_entries": [],
                }
            )
        )
    cat._body_store._cache_maxsize = 64
    with patch.object(components_module, "_COMPONENT_BODIES_DIR", bodies_dir):
        for i in range(70):
            await cat.get_body(f"comp_{i}")

    # 70 reads with maxsize=64 ⇒ first ~6 entries evicted.
    assert len(cat._body_store._cache) == 64
    assert "comp_0" not in cat._body_store._cache
    assert "comp_69" in cat._body_store._cache


async def test_get_component_bodies_returns_full_batch_larger_than_cache(
    tmp_path: Path,
) -> None:
    """A batch larger than the cache must still return every loaded body.

    Pins the correctness contract that the cache is a hot-read
    optimization, not a result store: an early entry can get
    evicted by the LRU loop during the same batch, but the caller
    must still see it in the returned dict. Regression guard for
    the silent-drop bug that would otherwise hit a navigator
    mounting >MAXSIZE components.
    """
    cat = ComponentCatalog()
    cat._by_id = {f"comp_{i}": _make_entry(entry_id=f"comp_{i}") for i in range(200)}
    cat._components = list(cat._by_id.values())
    bodies_dir = tmp_path / "components"
    bodies_dir.mkdir()
    for i in range(200):
        (bodies_dir / f"comp_{i}.json").write_text(
            json.dumps(
                {
                    "id": f"comp_{i}",
                    "name": f"comp_{i}",
                    "category": "misc",
                    "description": "",
                    "config_entries": [],
                }
            )
        )
    cat._body_store._cache_maxsize = 32
    with patch.object(components_module, "_COMPONENT_BODIES_DIR", bodies_dir):
        result = await cat.get_component_bodies(
            component_ids=[f"comp_{i}" for i in range(200)],
        )

    assert len(result) == 200
    # Cache trimmed; older entries evicted by the time the batch returned.
    assert len(cat._body_store._cache) == 32
    # But the result dict held the references, so the early ids are still present.
    assert result["comp_0"].id == "comp_0"
    assert result["comp_199"].id == "comp_199"


async def test_get_body_refuses_path_traversal_id(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A traversal-shaped id is rejected by the loader even if it slips past the index check.

    Pins the defense-in-depth path-traversal guard in
    ``_load_body_from_disk``. The id check in ``get_body`` (``not in
    self._by_id``) is the first line of defence; the loader's
    ``is_relative_to`` guard makes the safety property local so it
    survives any future change that leaks an attacker-controllable
    id into ``_by_id``.
    """
    cat = ComponentCatalog()
    # Plant the traversal id directly in the index so get_body
    # proceeds past its own guard and reaches the loader.
    cat._by_id = {"../escape": _make_entry(entry_id="../escape")}
    bodies_dir = tmp_path / "components"
    bodies_dir.mkdir()
    # Drop a file at the would-be-escape target so the test would
    # incorrectly succeed if the guard were missing.
    (tmp_path / "escape.json").write_text(
        json.dumps(
            {
                "id": "escape",
                "name": "escape",
                "category": "misc",
                "description": "",
                "config_entries": [],
            }
        )
    )
    with (
        patch.object(components_module, "_COMPONENT_BODIES_DIR", bodies_dir),
        caplog.at_level(logging.WARNING),
    ):
        result = await cat.get_body("../escape")

    assert result is None
    assert any("traversal-shaped id" in rec.message for rec in caplog.records)


async def test_get_body_returns_none_when_body_missing_on_disk(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Index says yes, disk says no — return ``None`` with a warning."""
    cat = ComponentCatalog()
    cat._by_id = {"phantom": _make_entry(entry_id="phantom")}
    bodies_dir = tmp_path / "components"
    bodies_dir.mkdir()
    with (
        patch(
            "esphome_device_builder.controllers.components._resolve._COMPONENT_BODIES_DIR",
            bodies_dir,
        ),
        caplog.at_level(logging.WARNING),
    ):
        result = await cat.get_body("phantom")

    assert result is None
    assert any("body missing on disk" in rec.message for rec in caplog.records)


async def test_get_body_coalesces_concurrent_calls_into_one_disk_read(
    tmp_path: Path,
) -> None:
    """Two ``get_body`` calls for the same id share one disk read.

    Pins the lock-plus-recheck coalescing contract: the batch
    endpoint and a concurrent singleton fetch must not both
    schedule a thread-pool read for the same component.
    """
    cat = ComponentCatalog()
    cat._by_id = {"wifi": _make_entry(entry_id="wifi")}
    bodies_dir = tmp_path / "components"
    bodies_dir.mkdir()
    (bodies_dir / "wifi.json").write_text(
        json.dumps(
            {
                "id": "wifi",
                "name": "Wi-Fi",
                "category": "core",
                "description": "",
                "config_entries": [],
            }
        )
    )

    call_count = 0
    real_loader = components_module._load_body_from_disk

    def _counting_loader(component_id: str):
        nonlocal call_count
        call_count += 1
        return real_loader(component_id)

    with (
        patch.object(components_module, "_COMPONENT_BODIES_DIR", bodies_dir),
        patch.object(cat._body_store, "_load_one", _counting_loader),
    ):
        first, second = await asyncio.gather(cat.get_body("wifi"), cat.get_body("wifi"))

    assert first is second
    assert call_count == 1


async def test_get_component_bodies_bulk_loads_in_one_executor_hop(
    tmp_path: Path,
) -> None:
    """A batch of N ids dispatches one ``asyncio.to_thread``, not N.

    Regression guard against the per-id-future / asyncio.gather
    shape this endpoint started with; #939 review pointed out that
    N small executor jobs thrashes the thread pool. The fix is one
    executor hop that reads every missing body sequentially.
    """
    cat = ComponentCatalog()
    cat._by_id = {f"comp_{i}": _make_entry(entry_id=f"comp_{i}") for i in range(10)}
    cat._components = list(cat._by_id.values())
    bodies_dir = tmp_path / "components"
    bodies_dir.mkdir()
    for i in range(10):
        (bodies_dir / f"comp_{i}.json").write_text(
            json.dumps(
                {
                    "id": f"comp_{i}",
                    "name": f"comp_{i}",
                    "category": "misc",
                    "description": "",
                    "config_entries": [],
                }
            )
        )

    to_thread_calls = 0
    real_to_thread = asyncio.to_thread

    async def _counting_to_thread(func, /, *args, **kwargs):
        nonlocal to_thread_calls
        to_thread_calls += 1
        return await real_to_thread(func, *args, **kwargs)

    with (
        patch.object(components_module, "_COMPONENT_BODIES_DIR", bodies_dir),
        patch.object(asyncio, "to_thread", _counting_to_thread),
    ):
        result = await cat.get_component_bodies(component_ids=[f"comp_{i}" for i in range(10)])

    assert len(result) == 10
    assert to_thread_calls == 1


# ── get_component_bodies() ──────────────────────────────────────────


async def test_load_bodies_dedupes_repeated_ids_before_disk_read(
    tmp_path: Path,
) -> None:
    """Repeated ids in the input collapse to one disk read.

    ``resolve_default_components`` may pass the same underlying id
    twice when a board lists the same component under multiple
    featured refs. The loader must not re-read the same body file
    just because the caller's input list has duplicates.
    """
    cat = ComponentCatalog()
    cat._by_id = {"wifi": _make_entry(entry_id="wifi")}
    bodies_dir = tmp_path / "components"
    bodies_dir.mkdir()
    (bodies_dir / "wifi.json").write_text(
        json.dumps(
            {
                "id": "wifi",
                "name": "Wi-Fi",
                "category": "core",
                "description": "",
                "config_entries": [],
            }
        )
    )

    call_count = 0
    real_loader = components_module._load_body_from_disk

    def _counting_loader(component_id: str):
        nonlocal call_count
        call_count += 1
        return real_loader(component_id)

    with (
        patch.object(components_module, "_COMPONENT_BODIES_DIR", bodies_dir),
        patch.object(cat._body_store, "_load_one", _counting_loader),
    ):
        result = await cat._load_bodies(["wifi", "wifi", "wifi"])

    assert set(result) == {"wifi"}
    assert call_count == 1


async def test_get_component_bodies_skips_featured_with_missing_body(
    tmp_path: Path,
) -> None:
    """A featured ref whose underlying body wasn't loaded drops out silently.

    Pins the ``_resolve_one_from_bodies`` defensive branch: when the
    featured registry has a record but its underlying body never
    made it into the load batch (e.g. the body file was deleted
    mid-sync), the entry is simply absent from the result rather
    than throwing.
    """
    cat = ComponentCatalog()
    cat._by_id = {"switch.gpio": _make_entry(entry_id="switch.gpio")}
    cat._components = list(cat._by_id.values())
    cat._featured_by_id = {
        "featured.test-board.relay": _FeaturedRecord(
            full_id="featured.test-board.relay",
            board_id="test-board",
            featured=FeaturedComponent(id="relay", component_id="switch.gpio"),
            underlying_id="switch.gpio",
        )
    }
    bodies_dir = tmp_path / "components"
    bodies_dir.mkdir()
    # NOTE: deliberately don't write switch.gpio.json — the body
    # load returns nothing, so the featured resolve should bail.
    with patch.object(components_module, "_COMPONENT_BODIES_DIR", bodies_dir):
        result = await cat.get_component_bodies(
            component_ids=["featured.test-board.relay"],
        )

    assert result == {}


async def test_get_component_bodies_returns_dict_keyed_by_id(tmp_path: Path) -> None:
    """Batch hydrate returns one entry per known id; unknown ids drop out."""
    cat = ComponentCatalog()
    cat._by_id = {
        "wifi": _make_entry(entry_id="wifi"),
        "api": _make_entry(entry_id="api"),
    }
    cat._components = list(cat._by_id.values())
    bodies_dir = tmp_path / "components"
    bodies_dir.mkdir()
    for cid in ("wifi", "api"):
        (bodies_dir / f"{cid}.json").write_text(
            json.dumps(
                {
                    "id": cid,
                    "name": cid,
                    "category": "core",
                    "description": "",
                    "config_entries": [],
                }
            )
        )
    with patch(
        "esphome_device_builder.controllers.components._resolve._COMPONENT_BODIES_DIR",
        bodies_dir,
    ):
        result = await cat.get_component_bodies(
            component_ids=["wifi", "api", "does-not-exist", "wifi"],
        )

    assert set(result) == {"wifi", "api"}
    assert result["wifi"].id == "wifi"
    assert result["api"].id == "api"


def test_materialise_entry_resolves_platform_default() -> None:
    """A matching ``target_platform`` swaps in its ``platform_defaults`` value and drops the map."""
    entry = ConfigEntry(
        key="baud_rate",
        type=ConfigEntryType.INTEGER,
        label="Baud rate",
        default_value=9600,
        platform_defaults={"esp32": 115200, "esp8266": 57600},
    )

    resolved = components_module._materialise_entry(entry, "esp32")

    assert resolved.default_value == 115200
    assert resolved.platform_defaults is None


def test_materialise_entry_keeps_default_when_platform_absent() -> None:
    """A ``target_platform`` absent from ``platform_defaults`` keeps ``default_value``."""
    entry = ConfigEntry(
        key="baud_rate",
        type=ConfigEntryType.INTEGER,
        label="Baud rate",
        default_value=9600,
        platform_defaults={"esp32": 115200},
    )

    resolved = components_module._materialise_entry(entry, "rp2040")

    assert resolved.default_value == 9600
    assert resolved.platform_defaults is None


def test_get_pin_registry_modes_returns_cached_map() -> None:
    """The endpoint returns the loaded ``{registry: [modes]}`` map verbatim."""
    cat = ComponentCatalog()
    cat._pin_registry_modes = {"pca9554": ["input", "output"]}

    assert asyncio.run(cat.get_pin_registry_modes()) == {"pca9554": ["input", "output"]}


def test_load_populates_pin_registry_modes(tmp_path: Path) -> None:
    """``load()`` caches the per-registry mode map from the loader."""
    index_path = tmp_path / "components.index.json"
    index_path.write_text(json.dumps({"components": []}))
    cat = ComponentCatalog()
    with (
        patch(
            "esphome_device_builder.controllers.components.controller._COMPONENTS_INDEX_JSON",
            index_path,
        ),
        patch(
            "esphome_device_builder.controllers.components.controller."
            "load_pin_registry_modes_index",
            return_value={"esp32": ["input", "output", "pullup"]},
        ),
    ):
        cat.load()

    assert cat._pin_registry_modes == {"esp32": ["input", "output", "pullup"]}
