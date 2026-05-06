"""Benchmarks for the dashboard startup hot path.

``DeviceBuilder.start()`` blocks on two synchronous catalog loads
before the first WS frame can be served: ``BoardCatalog.load()``
walks ~500 hand-curated ``manifest.yaml`` files under
``definitions/boards/`` and parses each via ``yaml.safe_load``;
``ComponentCatalog.load()`` decodes the ~20 MB pre-generated
``definitions/components.json`` and instantiates ~900
``ComponentCatalogEntry`` objects. Together they account for the
bulk of the wall-time gap a user feels comparing the new
dashboard's startup against the legacy Tornado one — and on
constrained hardware (HA Green) the absolute number runs into
tens of seconds.

Each benchmark below measures **one unit of work** that the
production loaders multiply across every entry — one manifest
parse, one ``_load_component`` dataclass build. That keeps the
per-iteration cost in the microsecond / sub-millisecond range
CodSpeed's simulation (callgrind) mode tolerates, while still
catching the per-unit regressions that compound 500x / 900x in
production. Benchmarking the full catalog loads end-to-end ran
into multi-minute callgrind runs that timed out CI.

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

_DEFINITIONS = Path(__file__).resolve().parents[2] / "esphome_device_builder" / "definitions"

# A typical mid-sized real board manifest (~160 lines, ESP32-C3
# DevKit + featured_components + tags + pins). Exercises every
# ``_load_*`` helper the per-board path runs in production.
# Cached as bytes so the benchmark loop measures parse + build,
# not the cold disk read.
_BOARD_MANIFEST_BYTES = (
    _DEFINITIONS / "boards" / "seeed-xiao-esp32c3" / "manifest.yaml"
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
    """Pin the per-board parse cost — the unit ``BoardCatalog.load()`` repeats ~500x.

    Production walks ``definitions/boards/*/manifest.yaml`` and
    runs ``yaml.safe_load`` + the chain of ``_load_*`` helpers on
    each. That per-file work is the dominant startup cost on
    constrained hardware (HA Green, see issue #368) where
    PyYAML's pure-Python parse loop hurts most. A regression here
    multiplies linearly across the full catalog, so a 10%
    slowdown on this benchmark is a 10% slowdown on dashboard
    startup wall-time.

    Run the YAML parse + every ``_load_*`` helper inline rather
    than calling ``load_board_catalog`` itself — the catalog
    function is a directory walk + per-file dispatch loop whose
    per-iteration cost we already cover here, and benchmarking
    the walk would re-pay disk I/O on every iteration.
    """
    board_id = "seeed-xiao-esp32c3"

    @benchmark
    def run() -> None:
        data = yaml.safe_load(_BOARD_MANIFEST_BYTES)
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

    @benchmark
    def run() -> None:
        _load_component(_SAMPLE_COMPONENT)
