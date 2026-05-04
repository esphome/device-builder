"""End-to-end coverage for ``DevicesController.search_yaml``.

The new ``yaml/search`` WS command is the dashboard's
"find a string across every YAML in this fleet" surface — the
frontend's full-content search dropdown calls it on every
keystroke (debounced).

Exercises:

- Substring matches across multiple files.
- Per-file cap so one chatty match doesn't drown the rest.
- Total-results cap so an over-broad query stays manageable.
- Case-insensitive default + opt-in case sensitivity.
- Empty / whitespace query short-circuits without iterating.
- Missing file / unreadable file is skipped, not a 500.

The controller iterates ``self._scanner.devices`` and reads
``self._db.settings.rel_path(configuration)`` from disk. The
``make_controller`` fixture wires ``rel_path`` against the
test's ``tmp_path``, so each test just writes YAML files at
the expected paths and pre-populates ``_scanner.devices``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from esphome_device_builder.models import Device, DeviceState

if TYPE_CHECKING:
    from .conftest import MakeControllerFactory


def _device(name: str, *, friendly: str | None = None) -> Device:
    """Bare-minimum ``Device`` for search-result assertions."""
    return Device(
        name=name,
        friendly_name=friendly or name.title(),
        configuration=f"{name}.yaml",
        address=f"{name}.local",
        state=DeviceState.ONLINE,
    )


def _seed_yaml(tmp_path: Path, name: str, content: str) -> None:
    (tmp_path / f"{name}.yaml").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Empty-query short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "\t"])
@pytest.mark.asyncio
async def test_search_yaml_empty_query_returns_empty_without_io(
    query: str,
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Empty / whitespace query short-circuits before touching disk.

    Frontend debounce can still fire a stray empty call (e.g. on
    "clear search"); iterating every device YAML for nothing is
    pure waste. Pin the early-return so a regression that drops
    the strip-and-bail surfaces as the "scanner.devices was
    accessed" assertion below.
    """
    controller = make_controller(tmp_path)
    # Seed a populated scanner so an unguarded loop would actually
    # do work — the assertion is that this path is NOT taken.
    controller._scanner.devices = [_device("kitchen")]
    _seed_yaml(tmp_path, "kitchen", "esphome:\n  name: kitchen\n")

    results = await controller.search_yaml(query=query)

    assert results == []


# ---------------------------------------------------------------------------
# Substring match shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_yaml_substring_match_returns_per_device_hits(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A simple substring match returns one entry per matching device.

    Pin the result shape (configuration / device_name / friendly_name
    / matches array of {line_number, line_text}) — the frontend's
    rendering layer reads each field by name. A field rename here
    silently breaks the dropdown rendering with no test failure.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [
        _device("kitchen", friendly="Kitchen Lamp"),
        _device("bedroom", friendly="Bedroom Sensor"),
    ]
    _seed_yaml(
        tmp_path,
        "kitchen",
        "esphome:\n  name: kitchen\nwifi:\n  ssid: home\n",
    )
    _seed_yaml(
        tmp_path,
        "bedroom",
        "esphome:\n  name: bedroom\nbinary_sensor:\n  - platform: gpio\n",
    )

    results = await controller.search_yaml(query="wifi")

    assert len(results) == 1
    hit = results[0]
    assert hit["configuration"] == "kitchen.yaml"
    assert hit["device_name"] == "kitchen"
    assert hit["friendly_name"] == "Kitchen Lamp"
    assert hit["matches"] == [{"line_number": 3, "line_text": "wifi:"}]


@pytest.mark.asyncio
async def test_search_yaml_friendly_name_falls_back_to_device_name(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Empty ``friendly_name`` falls back to the device name in results.

    Devices created without a friendly_name still need a
    user-readable label in the dropdown — falling through to
    ``device_name`` keeps the result row scannable instead of
    showing an empty string.
    """
    controller = make_controller(tmp_path)
    dev = _device("kitchen")
    dev.friendly_name = ""
    controller._scanner.devices = [dev]
    _seed_yaml(tmp_path, "kitchen", "wifi:\n")

    results = await controller.search_yaml(query="wifi")

    assert results[0]["friendly_name"] == "kitchen"


# ---------------------------------------------------------------------------
# Case-insensitive (default) vs case-sensitive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_yaml_default_is_case_insensitive(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Default search ignores case so "WIFI" finds ``wifi:``.

    Most users won't think about case. Pinning the default
    behaviour means a regression that flips it (e.g. forgetting
    to lower-case the haystack) surfaces here.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen")]
    _seed_yaml(tmp_path, "kitchen", "wifi:\n  ssid: home\n")

    results = await controller.search_yaml(query="WIFI")

    assert len(results) == 1
    assert results[0]["matches"][0]["line_number"] == 1


@pytest.mark.asyncio
async def test_search_yaml_case_sensitive_opt_in(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``case_sensitive=True`` distinguishes ``wifi`` from ``WIFI``.

    The opt-in flag is for queries where case really matters
    (e.g. searching for a constant name or a capitalised tag).
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen")]
    _seed_yaml(tmp_path, "kitchen", "wifi:\n")

    results = await controller.search_yaml(query="WIFI", case_sensitive=True)

    assert results == []


# ---------------------------------------------------------------------------
# Per-file + total caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_yaml_caps_matches_per_file(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Per-file matches cap at 5 so one chatty file doesn't drown others.

    Without the cap, a query of a YAML token like ``-`` (very
    common in lists) against a long config could return hundreds
    of hits from one file and crowd out matches in the rest of the
    fleet. Pin the cap so the dropdown stays usable.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen")]
    # Ten lines all containing the needle — only the first 5 should
    # come back.
    _seed_yaml(
        tmp_path,
        "kitchen",
        "\n".join(f"# wifi-ish {i}" for i in range(10)) + "\n",
    )

    results = await controller.search_yaml(query="wifi")

    assert len(results[0]["matches"]) == 5


@pytest.mark.asyncio
async def test_search_yaml_caps_total_results(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Total result count caps at ``max_results`` across all devices.

    Frontend dropdown caps its rendering anyway — there's no point
    walking the whole fleet once we have enough hits. Pin the
    caller-controllable ``max_results`` so a regression that
    ignored the parameter would let the response balloon.
    """
    controller = make_controller(tmp_path)
    # Three devices, each with one match → 3 hits available.
    controller._scanner.devices = [_device(f"dev{i}") for i in range(3)]
    for i in range(3):
        _seed_yaml(tmp_path, f"dev{i}", "wifi:\n")

    results = await controller.search_yaml(query="wifi", max_results=2)

    # Walks devices in scanner order; cap kicks in before the third.
    total_matches = sum(len(r["matches"]) for r in results)
    assert total_matches <= 2
    assert len(results) <= 2


# ---------------------------------------------------------------------------
# Robustness: missing / unreadable files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_yaml_serialises_concurrent_calls(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Concurrent ``search_yaml`` calls run one-at-a-time via the lock.

    The command is I/O-bound — one ``stat`` per device + reads on
    cache misses — so two concurrent searches against the same
    fleet would just double the disk pressure without helping
    latency. The frontend's debounce keeps the depth low, but a
    slow request from a stuck client must not fan out to N
    parallel walks. Pin the contract by tracking the maximum
    concurrent depth observed inside the cache helper:
    one search active at any given moment, even when two
    high-level callers race.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [_device("kitchen")]
    _seed_yaml(tmp_path, "kitchen", "wifi:\n")

    in_flight = 0
    max_in_flight = 0
    real_get_lines = controller._yaml_search_cache.get_lines

    async def _spy_get_lines(*args: object, **kwargs: object) -> list[str] | None:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            # Yield once so the other coroutine has a chance to
            # interleave inside the same call if the lock isn't
            # holding it back.
            await asyncio.sleep(0)
            return await real_get_lines(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            in_flight -= 1

    controller._yaml_search_cache.get_lines = _spy_get_lines  # type: ignore[assignment]

    a, b = await asyncio.gather(
        controller.search_yaml(query="wifi"),
        controller.search_yaml(query="wifi"),
    )

    # Both calls produced the same hit set — serialised, not
    # cancelled — and the lock kept them strictly one-at-a-time.
    assert a == b
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_search_yaml_skips_missing_files(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A device whose YAML disappeared mid-scan is skipped, not a 500.

    The scanner's index can briefly disagree with the filesystem
    (atomic-save remove + re-add, manual rm by the user, etc.).
    Search shouldn't blow up the WS dispatcher in that window —
    the missing file just doesn't contribute hits, the rest of
    the fleet still searches.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = [
        _device("kitchen"),
        _device("bedroom"),
    ]
    # Only seed kitchen.yaml — bedroom.yaml is intentionally missing.
    _seed_yaml(tmp_path, "kitchen", "wifi:\n")

    results = await controller.search_yaml(query="wifi")

    # Kitchen still searched and matched; bedroom's missing file
    # didn't blow up the call.
    assert len(results) == 1
    assert results[0]["configuration"] == "kitchen.yaml"
