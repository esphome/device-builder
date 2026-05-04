"""Benchmarks for the ``yaml/search`` hot path.

The dashboard's YAML-content search fires once per debounced
keystroke. The actual *work* is the per-file substring scan in
``scan_lines`` — everything else (asyncio dispatch, cache
lookups, event-loop yields) is plumbing whose overhead would
otherwise drown the signal.

These benchmarks call ``scan_lines`` directly, synchronously,
against a representative ~5k-line ESPHome YAML. That isolates
the cost of the per-line work — ``str.lower`` (case-insensitive
default), substring-in check, dict allocation on a hit — so a
regression in the hot path surfaces with a clean number in
CodSpeed instead of getting smeared across asyncio.run +
cache-lock + sleep(0)-yield overhead.

Five shapes:

- *No match, case-insensitive* — the worst case, no early
  break, ``str.lower`` per line.
- *No match, case-sensitive* — same shape, no lower. Delta
  against the insensitive run measures the cost of the
  per-line lower; useful signal if we ever cache a pre-lowered
  copy.
- *Match capped early* — common token, the per-file cap fires
  within the first few sensors. Pins the early-break.
- *Fleet of 20x 5k-line* — scaled walk, surfaces any
  per-device fixed cost beyond the line scan itself.

The async ``search_yaml_devices`` end-to-end (with
``YamlSearchCache`` + ``asyncio.run``) lives in the unit-test
suite — including there in CodSpeed would just measure the
event-loop machinery.
"""

from __future__ import annotations

from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.controllers.devices._yaml_search import (
    MAX_LINES_PER_FILE,
    scan_lines,
)

# Pin the benchmark fixture's line count to the production
# pathological-file cap so the two stay in sync — if the cap
# moves, the benchmark moves with it and we keep measuring
# "the largest file the search loop actually scans".
_TARGET_LINES = MAX_LINES_PER_FILE


def _generate_yaml(target_lines: int) -> list[str]:
    """Build a ~target-line ESPHome YAML and pre-split into lines.

    Mostly ``binary_sensor`` entries — that's the shape large
    packaged configs (ratgdo / Apollo / etc) produce, dozens of
    GPIO-keyed sensors with names + ids + filter blocks. Each
    block is ~6 lines, so 5000 lines ≈ 800 sensors after the
    leading boilerplate.

    Returns the line list directly (not a string) — that matches
    what ``YamlSearchCache.get_lines`` would return after one
    cold call, which is the input shape ``scan_lines`` consumes.
    """
    parts = [
        "esphome:",
        "  name: bench_device",
        "  friendly_name: Bench Device",
        "  min_version: 2025.2.1",
        "",
        "esp32:",
        "  board: esp32-c3-devkitm-1",
        "  framework:",
        "    type: esp-idf",
        "",
        "wifi:",
        "  ssid: !secret wifi_ssid",
        "  password: !secret wifi_password",
        "",
        "api:",
        "logger:",
        "  level: INFO",
        "ota:",
        "  - platform: esphome",
        "",
        "binary_sensor:",
    ]
    needed = max(0, (target_lines - len(parts)) // 6)
    for i in range(needed):
        parts.append("  - platform: gpio")
        parts.append(f"    pin: GPIO{i % 30}")
        parts.append(f'    name: "Bench Sensor {i:04d}"')
        parts.append(f"    id: sensor_{i:04d}")
        parts.append("    filters:")
        parts.append("      - delayed_on: 50ms")
    return parts


_LINES_5K = _generate_yaml(_TARGET_LINES)
_FLEET_20 = [_LINES_5K] * 20

# Sentinel that does not appear anywhere in the generated YAML.
_NO_MATCH = "tag-that-matches-no-line-anywhere"
# A token present on every binary_sensor block — first hit
# lands within ~25 lines, so the per-file cap of 5 fires
# within the first ~150 lines regardless of file size.
_COMMON = "platform"


# ---------------------------------------------------------------------------
# Single-file scan
# ---------------------------------------------------------------------------


def test_scan_5k_no_match_case_insensitive(benchmark: BenchmarkFixture) -> None:
    """5k-line file, no-match needle, default case-insensitive scan.

    Worst case: every line lowered, every line substring-checked,
    no early break. The slowest realistic shape — pin it so a
    regression in the line loop is visible.
    """

    @benchmark
    def run() -> None:
        result = scan_lines(_LINES_5K, _NO_MATCH, case_sensitive=False, max_take=5)
        assert result == []


def test_scan_5k_no_match_case_sensitive(benchmark: BenchmarkFixture) -> None:
    """5k-line file, no-match needle, ``case_sensitive=True``.

    Same shape as the insensitive benchmark above, but skips
    the per-line ``str.lower``. The delta is the per-line lower
    cost — useful signal if we ever cache a pre-lowered copy.
    """

    @benchmark
    def run() -> None:
        result = scan_lines(_LINES_5K, _NO_MATCH, case_sensitive=True, max_take=5)
        assert result == []


def test_scan_5k_match_capped_early(benchmark: BenchmarkFixture) -> None:
    """5k-line file, common token, per-file cap fires early.

    ``platform`` appears on every binary_sensor block, so the cap
    of 5 lands within the first ~150 lines. Should NOT pay for
    scanning the rest of the 5k lines once we have 5 matches.
    Pin the early-break path.
    """

    @benchmark
    def run() -> None:
        result = scan_lines(_LINES_5K, _COMMON, case_sensitive=False, max_take=5)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# Fleet walk
# ---------------------------------------------------------------------------


def test_scan_fleet_20x5k_no_match(benchmark: BenchmarkFixture) -> None:
    """20x 5k-line scans, no matches anywhere.

    Each device gets its own ``scan_lines`` call. With no caps
    short-circuiting, every device pays the full per-line scan,
    so this benchmark gives a clean signal of the per-device
    walk cost — and any fixed per-call overhead in
    ``scan_lines`` itself (loop entry, max_take comparison)
    shows up multiplied by 20.
    """

    @benchmark
    def run() -> None:
        total = 0
        for lines in _FLEET_20:
            total += len(scan_lines(lines, _NO_MATCH, case_sensitive=False, max_take=5))
        assert total == 0
