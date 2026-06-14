"""Benchmarks for the dashboard startup hot path.

``DeviceBuilder.start()`` blocks on two synchronous catalog loads
before the first WS frame can be served: ``BoardCatalog.load()``
decodes the slim ``definitions/boards.index.json`` and instantiates
~490 ``BoardCatalogIndex`` objects (board bodies load lazily on
detail-view via ``LazyBodyStore``); ``ComponentCatalog.load()``
decodes ``definitions/components.index.json`` and instantiates
~900 ``ComponentCatalogIndexEntry`` objects. Component / board
bodies load lazily; this bench still measures the per-entry cost
via ``ComponentCatalogEntry.from_dict`` /
``BoardCatalogEntry.from_dict`` against one body file each.

The per-board YAML parse benchmark covers ``script/sync_boards.py``
rather than the runtime path — a regression in the libyaml loader
chain or the per-board ``_load_*`` helpers would land silently
otherwise.

The fixture inputs are pre-loaded once at module-collection time
(real bytes from the bundled ``definitions/`` tree) so disk I/O
isn't sampled inside the benchmark — same shape as the
``_LINES_5K`` payload in ``test_yaml_search.py``.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.definitions import (
    _load_esphome_config,
    _load_featured_component,
    _load_hardware,
    _load_pin,
    _parse_tags,
)
from esphome_device_builder.helpers.json import loads
from esphome_device_builder.helpers.yaml import FastestSafeLoader
from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardCatalogIndex,
    ComponentCatalogEntry,
)

_DEFINITIONS = Path(__file__).resolve().parents[2] / "esphome_device_builder" / "definitions"

# Pre-decoded JSON dicts for the boards-load benchmarks. Reading the
# bytes once at collection time keeps disk I/O out of the
# per-iteration sample, matching the pattern used for the manifest
# bytes below.
_BOARDS_INDEX_DICT = loads((_DEFINITIONS / "boards.index.json").read_bytes())
# One representative board body — picked for its non-trivial pin
# table + featured components so the per-body cost the lazy loader
# repeats on every detail-view open is exercised.
_BOARD_BODY_DICT = loads(
    (_DEFINITIONS / "board_bodies" / "unexpectedmaker_feathers3d.json").read_bytes()
)

# A real board manifest picked to exercise *every* ``_load_*``
# helper the per-board path runs in production: hardware,
# pins, and featured_components are all populated. Cached as
# bytes so the benchmark loop measures parse + build, not the
# cold disk read.
_BOARD_DIR = _DEFINITIONS / "boards" / "unexpectedmaker_feathers3d"
_BOARD_MANIFEST_BYTES = (_BOARD_DIR / "manifest.yaml").read_bytes()

# A representative component dict from the live catalog. Picked
# for its non-trivial nesting — ``sensor.dht`` carries a handful
# of nested ``config_entries`` plus units / options, so the
# ``_load_config_entry`` recursion fires. Read directly from the
# per-id body file so the benchmark measures the per-entry
# dataclass-build cost the lazy-body path repeats on every
# detail-view open.
_SAMPLE_COMPONENT = loads((_DEFINITIONS / "components" / "sensor.dht.json").read_bytes())


def test_parse_one_board_manifest(benchmark: BenchmarkFixture) -> None:
    """Pin the per-board parse cost — the unit ``script/sync_boards.py`` repeats ~500x.

    The sync runs in CI and on every PR that touches a manifest,
    so a per-file regression compounds across the catalog and
    extends the round-trip diff check.

    Run the YAML parse + every ``_load_*`` helper inline rather
    than calling ``build_board_catalog_from_manifests`` itself —
    that function is a directory walk + per-file dispatch loop
    whose per-iteration cost we already cover here, and
    benchmarking the walk would re-pay disk I/O on every
    iteration.
    """
    board_id = "unexpectedmaker_feathers3d"

    # Smoke-validate the per-board pipeline ONCE outside the
    # benchmark loop so a refactor that turns ``_load_pin`` /
    # ``_load_featured_component`` into a no-op still fails the
    # test (instead of CodSpeed reporting a "speedup" against
    # nothing). Asserting *inside* @benchmark would inflate the
    # per-iteration cost the benchmark exists to measure. Counts
    # pinned to the fixture's current shape — update both if the
    # fixture board grows or shrinks an entry.
    # ``FastestSafeLoader`` is what production now uses (see
    # ``definitions.load_board_catalog``); benchmarking
    # ``yaml.safe_load`` would silently keep measuring the
    # pure-Python loader and miss the ~7-8x C-loader speedup.
    _smoke = yaml.load(_BOARD_MANIFEST_BYTES, Loader=FastestSafeLoader)  # noqa: S506
    assert len([_load_pin(p, board_id) for p in _smoke.get("pins", [])]) == 4
    assert (
        len(
            [
                _load_featured_component(fc, _BOARD_DIR)
                for fc in _smoke.get("featured_components", [])
            ]
        )
        == 5
    )

    @benchmark
    def run() -> None:
        data = yaml.load(_BOARD_MANIFEST_BYTES, Loader=FastestSafeLoader)  # noqa: S506
        _load_esphome_config(data["esphome"], board_id)
        _load_hardware(data.get("hardware"), board_id)
        _parse_tags(data.get("tags", []), board_id)
        for pin in data.get("pins", []):
            _load_pin(pin, board_id)
        for fc in data.get("featured_components", []):
            _load_featured_component(fc, _BOARD_DIR)


def test_load_one_component_entry(benchmark: BenchmarkFixture) -> None:
    """Pin the per-component dataclass-build cost — paid on every detail-view open.

    The catalog ships ~900 per-id body files behind a slim
    ``components.index.json``; the body files hydrate lazily on
    detail view through ``ComponentCatalog.get_body``. This bench
    measures the per-entry walk that builds a
    ``ComponentCatalogEntry`` (and recursively builds its
    ``ConfigEntry`` children) — the work that runs on every cache
    miss. ``sensor.dht`` is picked as a representative entry —
    non-trivial nested ``config_entries`` exercise the
    ``_load_config_entry`` recursion that's the bulk of the
    per-component cost.
    """
    # Validate the build path ONCE outside the loop so a refactor
    # that breaks the mashumaro builder fails this test. Asserting
    # inside @benchmark would inflate per-iteration cost.
    _smoke = ComponentCatalogEntry.from_dict(_SAMPLE_COMPONENT)
    assert _smoke.id == "sensor.dht"
    assert len(_smoke.config_entries) == 7

    @benchmark
    def run() -> None:
        ComponentCatalogEntry.from_dict(_SAMPLE_COMPONENT)


def test_load_board_index_json(benchmark: BenchmarkFixture) -> None:
    """Pin ``BoardCatalog.load()`` cost — only the slim index hydrates eagerly."""
    # The slim index is what dashboard startup actually pays for now;
    # the per-board bodies are lazy. A refactor that re-inlined the
    # body fields into the index would show up here as a large
    # regression rather than a silent disk + RAM bloat.
    smoke = [BoardCatalogIndex.from_dict(b) for b in _BOARDS_INDEX_DICT["boards"]]
    assert len(smoke) > 100

    @benchmark
    def run() -> None:
        [BoardCatalogIndex.from_dict(b) for b in _BOARDS_INDEX_DICT["boards"]]


def test_load_one_board_body_json(benchmark: BenchmarkFixture) -> None:
    """Pin per-body cost — what the LazyBodyStore pays on a detail-view open."""
    # One ``from_dict`` call covers the full BoardCatalogEntry tree
    # (hardware + pins + featured_components + featured_bundles +
    # default_components). Pins the cost the dashboard pays on every
    # cold board detail fetch.
    smoke = BoardCatalogEntry.from_dict(_BOARD_BODY_DICT)
    assert smoke.id == "unexpectedmaker_feathers3d"

    @benchmark
    def run() -> None:
        BoardCatalogEntry.from_dict(_BOARD_BODY_DICT)
