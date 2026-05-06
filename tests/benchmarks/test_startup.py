"""Benchmarks for the dashboard startup hot path.

``DeviceBuilder.start()`` blocks on two synchronous catalog loads
before the first WS frame can be served: ``BoardCatalog.load()``
deserializes the pre-generated ``definitions/boards.json`` via
``orjson`` + mashumaro (~30 ms locally for 492 boards);
``ComponentCatalog.load()`` decodes the ~20 MB pre-generated
``definitions/components.json`` and instantiates ~900
``ComponentCatalogEntry`` objects. Together they account for the
bulk of the wall-time gap a user feels comparing the new
dashboard's startup against the legacy Tornado one — and on
constrained hardware (HA Green) the absolute number runs into
tens of seconds.

Each benchmark below measures **one unit of work** that the
production loaders multiply across every entry — one
``ComponentCatalogEntry`` build, one full ``BoardCatalogResponse``
deserialize (the latter is one orjson decode + one mashumaro
``from_dict`` walk that recurses into all 492 boards). That keeps
the per-iteration cost in the microsecond / millisecond range
CodSpeed's simulation (callgrind) mode tolerates.

The per-board YAML parse benchmark is retained because
``script/sync_boards.py`` still exercises that path at sync time —
a regression in the libyaml loader chain or the per-board
``_load_*`` helpers would land silently otherwise.

The fixture inputs are pre-loaded once at module-collection time
(real bytes from the bundled ``definitions/`` tree) so disk I/O
isn't sampled inside the benchmark — same shape as the
``_LINES_5K`` payload in ``test_yaml_search.py``.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.controllers.components import _load_component
from esphome_device_builder.definitions import (
    _load_esphome_config,
    _load_featured_component,
    _load_hardware,
    _load_pin,
    _parse_tags,
)
from esphome_device_builder.helpers.json import loads
from esphome_device_builder.helpers.yaml import FastestSafeLoader
from esphome_device_builder.models import BoardCatalogResponse

_DEFINITIONS = Path(__file__).resolve().parents[2] / "esphome_device_builder" / "definitions"

# Pre-decoded JSON dict for the boards-load benchmark. Reading the
# bytes once at collection time keeps disk I/O out of the
# per-iteration sample, matching the pattern used for the manifest
# bytes below.
_BOARDS_JSON_DICT = loads((_DEFINITIONS / "boards.json").read_bytes())

# A real board manifest picked to exercise *every* ``_load_*``
# helper the per-board path runs in production: hardware,
# pins, and featured_components are all populated. Cached as
# bytes so the benchmark loop measures parse + build, not the
# cold disk read.
_BOARD_MANIFEST_BYTES = (
    _DEFINITIONS / "boards" / "unexpectedmaker_feathers3d" / "manifest.yaml"
).read_bytes()

# A representative component dict from the live catalog. Picked
# for its non-trivial nesting — ``sensor.dht`` carries a handful
# of nested ``config_entries`` plus units / options, so the
# ``_load_config_entry`` recursion fires. Pre-extracting one
# entry from the full catalog at collection time means the
# benchmark measures the per-entry dataclass-build cost the
# production load multiplies ~900x — not the one-shot orjson
# decode of the 20 MB blob, which doesn't realistically regress
# on its own and would dominate the callgrind sample.
_COMPONENTS_JSON_BYTES = (_DEFINITIONS / "components.json").read_bytes()
_SAMPLE_COMPONENT = next(
    c for c in loads(_COMPONENTS_JSON_BYTES)["components"] if c.get("id") == "sensor.dht"
)


def test_parse_one_board_manifest(benchmark: BenchmarkFixture) -> None:
    """Pin the per-board parse cost — the unit ``script/sync_boards.py`` repeats ~500x.

    Production no longer walks the YAML manifests at startup
    (see ``test_load_board_catalog_json`` below for that path),
    but ``script/sync_boards.py`` still does — this is the unit
    cost the sync script multiplies across every manifest when
    regenerating ``boards.json``. The sync runs in CI and on
    every PR that touches a manifest, so a per-file regression
    still matters even though it no longer hits dashboard
    startup directly.

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
    assert len([_load_featured_component(fc) for fc in _smoke.get("featured_components", [])]) == 5

    @benchmark
    def run() -> None:
        data = yaml.load(_BOARD_MANIFEST_BYTES, Loader=FastestSafeLoader)  # noqa: S506
        _load_esphome_config(data["esphome"], board_id)
        _load_hardware(data.get("hardware"), board_id)
        _parse_tags(data.get("tags", []), board_id)
        for pin in data.get("pins", []):
            _load_pin(pin, board_id)
        for fc in data.get("featured_components", []):
            _load_featured_component(fc)


def test_load_one_component_entry(benchmark: BenchmarkFixture) -> None:
    """Pin the per-component dataclass-build cost — repeated ~900x by ``ComponentCatalog.load()``.

    The 20 MB ``components.json`` decode is a single ``orjson``
    call that doesn't realistically regress on its own; the
    per-entry walk that builds a ``ComponentCatalogEntry`` (and
    recursively builds its ``ConfigEntry`` children) is the work
    that compounds across the catalog. ``sensor.dht`` is picked
    as a representative entry — non-trivial nested
    ``config_entries`` exercise the ``_load_config_entry``
    recursion that's the bulk of the per-component cost.
    """
    # Validate the build path ONCE outside the loop so a refactor
    # that stubs ``_load_config_entry`` to ``return None`` fails
    # the test. Asserting inside @benchmark would be a 30%+
    # overhead on a 500ns per-iteration cost — the loop body
    # stays clean.
    _smoke = _load_component(_SAMPLE_COMPONENT)
    assert _smoke.id == "sensor.dht"
    assert len(_smoke.config_entries) == 7

    @benchmark
    def run() -> None:
        _load_component(_SAMPLE_COMPONENT)


def test_load_board_catalog_json(benchmark: BenchmarkFixture) -> None:
    """Pin the production ``BoardCatalog.load()`` cost — one mashumaro deserialize.

    After issue #368, the dashboard reads the pre-generated
    ``definitions/boards.json`` at startup instead of walking
    ~500 manifest YAMLs. The cost is one ``orjson.loads`` (already
    paid once at module-collection time, so excluded from the
    sample) plus one ``BoardCatalogResponse.from_dict`` walk that
    instantiates 492 ``BoardCatalogEntry`` objects and all their
    nested dataclasses (pins, hardware, featured components,
    presets). A per-board regression in mashumaro's union dispatch
    or any of the model defaults would compound across the catalog
    just like the YAML loader regressions used to.
    """
    # Smoke check: deserialize once outside the loop so a refactor
    # that broke ``from_dict`` would surface here rather than via a
    # fast-but-empty catalog reading as a CodSpeed "speedup".
    smoke = BoardCatalogResponse.from_dict(_BOARDS_JSON_DICT)
    assert len(smoke.boards) > 100  # actual count is 492; floor lets test survive growth

    @benchmark
    def run() -> None:
        BoardCatalogResponse.from_dict(_BOARDS_JSON_DICT)
