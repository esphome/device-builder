"""Coverage for ``_yaml_search.search_yaml_devices``.

The end-to-end tests in ``test_search_yaml.py`` exercise the
loop via ``DevicesController.search_yaml`` (lock + cache prune
included). These tests pin the search loop in isolation:

- Substring match shape and the ``(results, live_configurations)``
  return tuple that the controller uses for cache pruning.
- ``case_sensitive`` flag — caller is responsible for pre-lowering
  the needle when False; pin both branches.
- Per-file cap so a chatty match doesn't crowd out other devices.
- Total-results cap stops the walk early; ``live_configurations``
  reflects only the devices actually walked, not the full input
  iterable.
- Empty / matches list → device skipped from results entirely.
- Cache miss (``get_lines`` returns ``None``) → device skipped
  from results, NOT from ``live_configurations`` (we still saw
  the device, just couldn't read its file).
- ``await asyncio.sleep(0)`` between devices — the no-blocking-I/O
  contract: a 100-device fleet's worth of in-memory line scans
  must not run as a single uninterrupted task slice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from esphome_device_builder.controllers.devices._yaml_search import (
    MAX_LINES_PER_FILE,
    search_yaml_devices,
)
from esphome_device_builder.controllers.devices._yaml_search_cache import (
    YamlSearchCache,
)


@dataclass
class _StubDevice:
    """Minimal _DeviceLike stand-in.

    Pins the Protocol's read-only contract — the function must
    not reach for any other attributes. A typo / rename in the
    real ``Device`` model that introduces a new dependency
    surfaces here as ``AttributeError`` immediately.
    """

    name: str
    friendly_name: str
    configuration: str


def _seed(tmp_path: Path, name: str, content: str) -> _StubDevice:
    (tmp_path / f"{name}.yaml").write_text(content, encoding="utf-8")
    return _StubDevice(name=name, friendly_name=name.title(), configuration=f"{name}.yaml")


def _rel(tmp_path: Path):
    return lambda c: tmp_path / c


# ---------------------------------------------------------------------------
# Match shape + return tuple
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_results_and_live_configurations(tmp_path: Path) -> None:
    """Result tuple shape — pinned for the controller's cache prune.

    The caller (``DevicesController.search_yaml``) uses the
    returned ``live_configurations`` set to evict stale cache
    entries for devices that have been deleted / archived. Pin
    that the set has exactly the device configurations we walked
    (whether or not they had matches).
    """
    cache = YamlSearchCache()
    devices = [
        _seed(tmp_path, "kitchen", "wifi:\n  ssid: home\n"),
        _seed(tmp_path, "bedroom", "binary_sensor:\n  - platform: gpio\n"),
    ]

    results, live = await search_yaml_devices(
        devices=devices,
        cache=cache,
        rel_path=_rel(tmp_path),
        needle="wifi",
        case_sensitive=False,
        max_results=50,
        per_file_cap=5,
    )

    assert len(results) == 1
    hit = results[0]
    assert hit["configuration"] == "kitchen.yaml"
    assert hit["device_name"] == "kitchen"
    assert hit["friendly_name"] == "Kitchen"
    assert hit["matches"] == [{"line_number": 1, "line_text": "wifi:"}]
    # Both devices walked → both in live_configurations, not just
    # the one with a match. Cache prune key.
    assert live == {"kitchen.yaml", "bedroom.yaml"}


@pytest.mark.asyncio
async def test_friendly_name_falls_back_to_device_name(tmp_path: Path) -> None:
    """Empty ``friendly_name`` → result row uses ``device_name``.

    Devices created without a friendly_name still need a
    user-readable label in the dropdown; pin the fallback so a
    refactor that drops the ``or device.name`` clause surfaces
    as a result-shape regression.
    """
    cache = YamlSearchCache()
    (tmp_path / "kitchen.yaml").write_text("wifi:\n", encoding="utf-8")
    devices = [_StubDevice(name="kitchen", friendly_name="", configuration="kitchen.yaml")]

    results, _ = await search_yaml_devices(
        devices=devices,
        cache=cache,
        rel_path=_rel(tmp_path),
        needle="wifi",
        case_sensitive=False,
        max_results=50,
        per_file_cap=5,
    )

    assert results[0]["friendly_name"] == "kitchen"


# ---------------------------------------------------------------------------
# Case sensitivity (caller pre-lowers)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case_insensitive_caller_lowered_needle(tmp_path: Path) -> None:
    """``case_sensitive=False`` lowers each line; needle is pre-lowered.

    The caller (``DevicesController.search_yaml``) lowers the
    needle once outside the loop so the per-line work isn't
    paying for an extra ``str.lower()`` on the query at every
    file. Pin that contract — passing an UPPER-cased needle
    with ``case_sensitive=False`` would silently miss matches.
    """
    cache = YamlSearchCache()
    devices = [_seed(tmp_path, "kitchen", "WIFI:\n")]

    results, _ = await search_yaml_devices(
        devices=devices,
        cache=cache,
        rel_path=_rel(tmp_path),
        needle="wifi",  # already lowered
        case_sensitive=False,
        max_results=50,
        per_file_cap=5,
    )

    assert len(results) == 1


@pytest.mark.asyncio
async def test_case_sensitive_distinguishes_case(tmp_path: Path) -> None:
    """``case_sensitive=True`` treats each line as-is."""
    cache = YamlSearchCache()
    devices = [_seed(tmp_path, "kitchen", "wifi:\n")]

    results, _ = await search_yaml_devices(
        devices=devices,
        cache=cache,
        rel_path=_rel(tmp_path),
        needle="WIFI",
        case_sensitive=True,
        max_results=50,
        per_file_cap=5,
    )

    assert results == []


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_lines_per_file_caps_pathological_files(tmp_path: Path) -> None:
    """Files past ``MAX_LINES_PER_FILE`` lines are scanned only up to the cap.

    Pathological configs (machine-generated YAML, runaway lambda
    blocks) would otherwise tie up the per-keystroke search loop
    while we walk tens of thousands of lines for a no-match
    needle. The cap defends the worst case — substring matches
    past line ``MAX_LINES_PER_FILE`` silently drop out, so the
    loop's hot path stays bounded.

    Pin the contract:
    - a match at the cap boundary IS returned (last in-range line);
    - a match past the cap is NOT returned (first out-of-range
      line should be invisible to search);
    - the line numbers in the result remain 1-indexed against the
      scanned slice (which is the same as the file line number,
      since we slice from line 1).
    """
    cache = YamlSearchCache()
    # ``MAX_LINES_PER_FILE`` lines of filler then two unique-needle
    # lines: the first lands exactly AT the cap (last scanned line),
    # the second lands past the cap (must be invisible).
    filler = "\n".join("# noise" for _ in range(MAX_LINES_PER_FILE - 1))
    content = f"{filler}\n# tag-at-cap\n# tag-past-cap\n"
    devices = [_seed(tmp_path, "huge", content)]

    results, _ = await search_yaml_devices(
        devices=devices,
        cache=cache,
        rel_path=_rel(tmp_path),
        needle="tag-at-cap",
        case_sensitive=False,
        max_results=50,
        per_file_cap=10,
    )
    assert results[0]["matches"] == [
        {"line_number": MAX_LINES_PER_FILE, "line_text": "# tag-at-cap"}
    ]

    # Same file, different needle that only appears past the cap.
    # ``cache`` is reused; the in-memory line list still holds the
    # full file (we cap the search, not the cache).
    results, _ = await search_yaml_devices(
        devices=devices,
        cache=cache,
        rel_path=_rel(tmp_path),
        needle="tag-past-cap",
        case_sensitive=False,
        max_results=50,
        per_file_cap=10,
    )
    assert results == []


@pytest.mark.asyncio
async def test_per_file_cap_truncates_matches(tmp_path: Path) -> None:
    """One device's match list caps at ``per_file_cap``."""
    cache = YamlSearchCache()
    devices = [_seed(tmp_path, "kitchen", "\n".join(f"# wifi {i}" for i in range(10)))]

    results, _ = await search_yaml_devices(
        devices=devices,
        cache=cache,
        rel_path=_rel(tmp_path),
        needle="wifi",
        case_sensitive=False,
        max_results=50,
        per_file_cap=3,
    )

    assert len(results[0]["matches"]) == 3


@pytest.mark.asyncio
async def test_total_results_cap_short_circuits_walk(tmp_path: Path) -> None:
    """Walk stops once ``max_results`` is reached.

    Pin both halves: total-matches cap is honoured, AND
    ``live_configurations`` reflects the *full* input device
    list — not just the walked subset. The cache-prune key
    contract: capped searches must not evict warm entries for
    unwalked-but-still-live devices, otherwise the next
    keystroke re-reads them from disk.
    """
    cache = YamlSearchCache()
    devices = [
        _seed(tmp_path, "a", "wifi:\n"),
        _seed(tmp_path, "b", "wifi:\n"),
        _seed(tmp_path, "c", "wifi:\n"),
        _seed(tmp_path, "d", "wifi:\n"),
    ]

    results, live = await search_yaml_devices(
        devices=devices,
        cache=cache,
        rel_path=_rel(tmp_path),
        needle="wifi",
        case_sensitive=False,
        max_results=2,
        per_file_cap=5,
    )

    total_matches = sum(len(r["matches"]) for r in results)
    assert total_matches <= 2
    # All four devices in the input list — ``c`` and ``d`` were
    # never walked because the cap was hit after ``a`` and ``b``,
    # but they're still live so prune() must not evict them.
    assert live == {"a.yaml", "b.yaml", "c.yaml", "d.yaml"}


# ---------------------------------------------------------------------------
# Skip behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmatched_device_omitted_from_results(tmp_path: Path) -> None:
    """A device with no matches is skipped from ``results``."""
    cache = YamlSearchCache()
    devices = [
        _seed(tmp_path, "kitchen", "wifi:\n"),
        _seed(tmp_path, "bedroom", "binary_sensor:\n"),
    ]

    results, live = await search_yaml_devices(
        devices=devices,
        cache=cache,
        rel_path=_rel(tmp_path),
        needle="wifi",
        case_sensitive=False,
        max_results=50,
        per_file_cap=5,
    )

    assert [r["configuration"] for r in results] == ["kitchen.yaml"]
    # ``bedroom`` walked but unmatched — still in
    # live_configurations so its cache entry doesn't get
    # spuriously evicted.
    assert live == {"kitchen.yaml", "bedroom.yaml"}


@pytest.mark.asyncio
async def test_unreadable_file_skipped_but_visited(tmp_path: Path) -> None:
    """Cache returns ``None`` → device skipped from results.

    The scanner's index can briefly disagree with the filesystem.
    A vanished YAML must not blow up the search — the cache's
    ``get_lines`` returns ``None`` and we move on to the next
    device. The device IS still in ``live_configurations`` (we
    visited it) so the cache prune step doesn't see it as a
    "deleted" device the very next call.
    """
    cache = YamlSearchCache()
    # Seed only kitchen.yaml — bedroom is intentionally absent.
    seed = _seed(tmp_path, "kitchen", "wifi:\n")
    bedroom = _StubDevice(name="bedroom", friendly_name="Bedroom", configuration="bedroom.yaml")
    devices = [seed, bedroom]

    results, live = await search_yaml_devices(
        devices=devices,
        cache=cache,
        rel_path=_rel(tmp_path),
        needle="wifi",
        case_sensitive=False,
        max_results=50,
        per_file_cap=5,
    )

    assert [r["configuration"] for r in results] == ["kitchen.yaml"]
    assert "bedroom.yaml" in live


# ---------------------------------------------------------------------------
# Event-loop yield
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yields_to_event_loop_between_devices(tmp_path: Path) -> None:
    """``await asyncio.sleep(0)`` between devices keeps the loop responsive.

    For a fully-warm cache (no disk I/O on subsequent searches),
    the per-device scan is in-memory and would otherwise run as
    a single uninterrupted task slice. The explicit yield gives
    the WS dispatcher and other tasks a chance to interleave.
    Pin the contract by counting how many times a sibling
    ``asyncio.sleep(0)``-driven counter ticks during the walk:
    at least once per device, since each device-loop iteration
    awaits before the next.
    """
    cache = YamlSearchCache()
    # Five devices, all with a match so the loop hits its
    # full per-device path (read + scan + match + yield).
    devices = [_seed(tmp_path, f"d{i}", "wifi:\n") for i in range(5)]
    # Pre-populate the cache so the search itself does no disk
    # I/O — any yields observed below come from the explicit
    # sleep(0) inside the loop, not from cache.get_lines.
    for d in devices:
        await cache.get_lines(d.configuration, tmp_path / d.configuration)

    ticks = 0
    stop = asyncio.Event()

    async def _ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0)

    ticker_task = asyncio.create_task(_ticker())
    try:
        await search_yaml_devices(
            devices=devices,
            cache=cache,
            rel_path=_rel(tmp_path),
            needle="wifi",
            case_sensitive=False,
            max_results=50,
            per_file_cap=5,
        )
    finally:
        stop.set()
        await ticker_task

    # At least one tick per device — the per-iteration yield
    # gave the ticker a chance to run between devices. Without
    # the yield, the search would have monopolised the slice
    # and the ticker would only run after the search returned.
    assert ticks >= len(devices)
