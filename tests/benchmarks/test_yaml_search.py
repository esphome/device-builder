"""Benchmarks for the ``yaml/search`` hot path.

The dashboard's YAML-content search fires one
``search_yaml_devices`` call per debounced keystroke. For a fleet
of typical-size configs (~500 lines / device) this is microsecond
work, but the interesting cases are at the edges:

- *Cold cache* — a freshly-loaded dashboard reads + splits every
  device's YAML once, then serves subsequent searches from memory.
  Per-device cost is dominated by ``Path.read_text`` +
  ``str.splitlines``.
- *Warm cache* — every subsequent keystroke does ``Path.stat`` per
  device and a substring scan per cached line list. Worst case
  here is a query that produces *no* matches anywhere — the
  per-file cap can't short-circuit, so the entire 5k-line line
  list is scanned for each device.
- *Case-insensitive scan* — pays an extra ``str.lower`` per line.
  Pinned separately because it's the documented "case_sensitive
  defaults to False" path the frontend uses.

Each benchmark uses a single ~5k-line representative ESPHome
YAML (binary_sensor-heavy, the shape large packaged configs take
in production) so a regression in the cache or in the search
loop's per-line work surfaces with a stable signal in CodSpeed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.controllers.devices._yaml_search import (
    search_yaml_devices,
)
from esphome_device_builder.controllers.devices._yaml_search_cache import (
    YamlSearchCache,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


_TARGET_LINES = 5000


@dataclass
class _StubDevice:
    """Minimal Device-shaped stand-in for the search loop's Protocol."""

    name: str
    friendly_name: str
    configuration: str


def _generate_yaml(target_lines: int) -> str:
    """Generate a ~target-line ESPHome YAML in production shape.

    Mostly ``binary_sensor`` entries — that's the shape large
    packaged configs (ratgdo / Apollo / etc) produce, dozens of
    GPIO-keyed sensors with names + ids + filter blocks. Each
    block is ~6 lines, so 5000 lines ≈ 800 sensors after the
    leading ``esphome:`` / ``wifi:`` / ``api:`` boilerplate.
    """
    parts = [
        "esphome:\n",
        "  name: bench_device\n",
        "  friendly_name: Bench Device\n",
        "  min_version: 2025.2.1\n",
        "\n",
        "esp32:\n",
        "  board: esp32-c3-devkitm-1\n",
        "  framework:\n",
        "    type: esp-idf\n",
        "\n",
        "wifi:\n",
        "  ssid: !secret wifi_ssid\n",
        "  password: !secret wifi_password\n",
        "\n",
        "api:\n",
        "logger:\n",
        "  level: INFO\n",
        "ota:\n",
        "  - platform: esphome\n",
        "\n",
        "binary_sensor:\n",
    ]
    block_lines = 6
    current = sum(p.count("\n") for p in parts)
    needed = max(0, (target_lines - current) // block_lines)
    for i in range(needed):
        parts.append("  - platform: gpio\n")
        parts.append(f"    pin: GPIO{i % 30}\n")
        parts.append(f'    name: "Bench Sensor {i:04d}"\n')
        parts.append(f"    id: sensor_{i:04d}\n")
        parts.append("    filters:\n")
        parts.append("      - delayed_on: 50ms\n")
    return "".join(parts)


def _seed(tmp: Path, count: int = 1, target_lines: int = _TARGET_LINES) -> list[_StubDevice]:
    """Write *count* generated YAMLs into *tmp* and return the device stubs."""
    yaml = _generate_yaml(target_lines)
    devices: list[_StubDevice] = []
    for i in range(count):
        name = f"bench{i:03d}"
        (tmp / f"{name}.yaml").write_text(yaml, encoding="utf-8")
        devices.append(
            _StubDevice(name=name, friendly_name=f"Bench {i}", configuration=f"{name}.yaml")
        )
    return devices


def _rel(tmp: Path):
    return lambda c: tmp / c


def _drive(
    devices: Iterable[_StubDevice],
    cache: YamlSearchCache,
    tmp: Path,
    needle: str,
    case_sensitive: bool = False,
) -> int:
    """Run one ``search_yaml_devices`` call and return the result count.

    The dashboard's debounce + lock means the search itself is the
    hot path; setup happens once outside the benchmarked block.
    Returning a count keeps the result alive so the optimiser
    can't elide the call.
    """

    async def _run() -> int:
        results, _ = await search_yaml_devices(
            devices=list(devices),
            cache=cache,
            rel_path=_rel(tmp),
            needle=needle,
            case_sensitive=case_sensitive,
            max_results=50,
            per_file_cap=5,
        )
        return len(results)

    return asyncio.run(_run())


def _warm(devices: list[_StubDevice], tmp: Path) -> YamlSearchCache:
    """Pre-populate a cache so subsequent searches hit the warm path."""
    cache = YamlSearchCache()

    async def _seed_cache() -> None:
        for d in devices:
            await cache.get_lines(d.configuration, tmp / d.configuration)

    asyncio.run(_seed_cache())
    return cache


# ---------------------------------------------------------------------------
# Cold: first search against a fresh cache (read + splitlines + scan)
# ---------------------------------------------------------------------------


def test_cold_5k_no_match(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    """Cold cache, query matches nothing — worst-case full-file scan.

    Pins the read-from-disk + splitlines + per-line scan path
    end-to-end. A regression in any of the three surfaces here.
    """
    devices = _seed(tmp_path)

    @benchmark
    def run() -> None:
        # Fresh cache per iteration so we always pay the cold cost.
        cache = YamlSearchCache()
        hits = _drive(devices, cache, tmp_path, "query_that_matches_no_line_anywhere")
        assert hits == 0


# ---------------------------------------------------------------------------
# Warm: subsequent searches against the populated cache
# ---------------------------------------------------------------------------


def test_warm_5k_no_match_case_insensitive(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    """Warm cache, no-match query, default case-insensitive scan.

    The frontend's default. Each iteration scans every cached
    line (no per-file cap short-circuit) and lowers each line
    before substring check. Slowest of the realistic shapes.
    """
    devices = _seed(tmp_path)
    cache = _warm(devices, tmp_path)

    @benchmark
    def run() -> None:
        hits = _drive(devices, cache, tmp_path, "query_that_matches_no_line_anywhere")
        assert hits == 0


def test_warm_5k_no_match_case_sensitive(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    """Warm cache, no-match query, ``case_sensitive=True``.

    Same shape as the case-insensitive benchmark but skips the
    per-line ``str.lower``. The delta against the insensitive
    benchmark above measures the cost of the lower-on-every-line
    overhead — useful signal if we ever cache a pre-lowered copy.
    """
    devices = _seed(tmp_path)
    cache = _warm(devices, tmp_path)

    @benchmark
    def run() -> None:
        hits = _drive(
            devices,
            cache,
            tmp_path,
            "query_that_matches_no_line_anywhere",
            case_sensitive=True,
        )
        assert hits == 0


def test_warm_5k_match_capped_early(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    """Warm cache, common token — per-file cap short-circuits.

    ``platform`` appears on every binary_sensor block, so the
    per-file cap (5) kicks in within the first few sensors of the
    file. Pins the early-break path: we should NOT pay for
    scanning the rest of the 5k lines once we've found 5 matches.
    """
    devices = _seed(tmp_path)
    cache = _warm(devices, tmp_path)

    @benchmark
    def run() -> None:
        hits = _drive(devices, cache, tmp_path, "platform")
        assert hits == 1


# ---------------------------------------------------------------------------
# Fleet: scaled to many devices to expose per-device overhead
# ---------------------------------------------------------------------------


def test_warm_fleet_20x5k_no_match(benchmark: BenchmarkFixture, tmp_path: Path) -> None:
    """20x 5k-line devices, warm cache, no matches — fleet-scaled walk.

    A small fleet of large configs is the realistic upper bound
    for a power user. With caps disabled (``no match``) every
    device pays the full per-line scan, so this benchmark gives
    a clean signal of the per-device walk cost — the dashboard's
    debounce can't hide regressions here.
    """
    devices = _seed(tmp_path, count=20)
    cache = _warm(devices, tmp_path)

    @benchmark
    def run() -> None:
        hits = _drive(devices, cache, tmp_path, "query_that_matches_no_line_anywhere")
        assert hits == 0
