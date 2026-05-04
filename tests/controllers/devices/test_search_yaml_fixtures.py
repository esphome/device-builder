"""End-to-end ``yaml/search`` against realistic ESPHome YAML fixtures.

The sibling ``test_search_yaml.py`` pins the controller-level
contracts (caps, case, locking, missing-file robustness) using
inlined synthetic YAML — small, focused, fast.

This file targets a different question: does the search behave
correctly against the *kind* of YAML users actually have on disk?
Multi-section configs with comments, anchors-style substitutions,
``!secret`` references, packages, ethernet vs wifi shapes,
``${var}`` interpolation, indented platform blocks. The fixtures
under ``tests/fixtures/yaml_search/`` are anonymised real configs
covering four common shapes:

- ``bluetooth_proxy.yaml`` — esp32-c3 + ``bluetooth_proxy:`` +
  ``esp32_ble_tracker:`` + buttons.
- ``smart_plug.yaml`` — ESP8266 + ``cse7766`` power monitoring +
  ``binary_sensor`` / ``sensor`` / ``switch`` / ``status_led``.
- ``packaged_device.yaml`` — ``packages: remote_package: …`` +
  ``substitutions:`` + ``${name}`` interpolation.
- ``ethernet_proxy.yaml`` — ``ethernet:`` (LAN8720) + ``bluetooth_proxy:``
  + ``time:`` / ``text_sensor:`` / ``sensor:`` / ``switch:``.

Confidence we want from these tests:

- A query for an ESPHome top-level key (``bluetooth_proxy``)
  hits the right subset of the fleet.
- Multi-line matches inside a block (``cse7766``'s
  ``current``/``voltage``/``power``) come back in file order with
  correct line numbers.
- Comment-only matches still surface (the YAML is the source of
  truth, comments included — users grepping for a TODO or a
  magic constant care about them).
- Substitution / package syntax (``${name}``, ``!secret``) is just
  text to the searcher and matches as such.
- Per-file cap kicks in even on a chatty fixture (the smart plug
  has many ``Smart Plug`` matches).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from esphome_device_builder.models import Device, DeviceState

if TYPE_CHECKING:
    from .conftest import MakeControllerFactory


_FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures" / "yaml_search"


def _seed_fleet(tmp_path: Path) -> list[Device]:
    """Copy every fixture into ``tmp_path`` and return matching ``Device``s.

    Each fixture's filename (minus ``.yaml``) becomes the device
    name — the controller's search reads ``rel_path(configuration)``
    via the ``make_controller`` fixture's lambda, so the YAML must
    actually live at ``tmp_path / <configuration>``. Returning
    ``Device``s rather than letting the test build them keeps the
    call sites short and ensures every fixture-backed test runs
    against the same fleet shape.
    """
    devices: list[Device] = []
    for fixture in sorted(_FIXTURES.glob("*.yaml")):
        name = fixture.stem
        target = tmp_path / fixture.name
        target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
        devices.append(
            Device(
                name=name,
                friendly_name=name.replace("_", " ").title(),
                configuration=fixture.name,
                address=f"{name}.local",
                state=DeviceState.ONLINE,
            )
        )
    return devices


# ---------------------------------------------------------------------------
# Fleet-wide section search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bluetooth_proxy_query_picks_out_proxy_devices(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``bluetooth_proxy`` matches the two proxy fixtures, not the plug.

    Both ``bluetooth_proxy.yaml`` and ``ethernet_proxy.yaml`` carry
    a top-level ``bluetooth_proxy:`` block; the smart plug doesn't.
    The packaged-device fixture imports its real config from a
    package and likewise has no inline ``bluetooth_proxy:``. Pin
    that fleet-wide section search returns the right subset.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = _seed_fleet(tmp_path)

    results = await controller.search_yaml(query="bluetooth_proxy")

    matched = {hit["configuration"] for hit in results}
    assert matched == {"bluetooth_proxy.yaml", "ethernet_proxy.yaml"}


@pytest.mark.asyncio
async def test_ethernet_query_isolates_the_ethernet_device(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``ethernet:`` matches only the ethernet fixture, not WiFi devices.

    The "find the device that doesn't have WiFi" use case — useful
    when an Ethernet-only device misbehaves and the user wants to
    jump to its config without scrolling the device list.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = _seed_fleet(tmp_path)

    results = await controller.search_yaml(query="ethernet")

    assert len(results) == 1
    assert results[0]["configuration"] == "ethernet_proxy.yaml"
    # The ``ethernet:`` block heading should be among the matches —
    # not just an incidental substring elsewhere in the file.
    line_texts = [m["line_text"] for m in results[0]["matches"]]
    assert any(text.strip() == "ethernet:" for text in line_texts)


# ---------------------------------------------------------------------------
# Block-internal multi-line matches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cse7766_block_returns_multiple_lines_in_order(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Multi-line block matches come back in file order with right line numbers.

    The smart-plug fixture's ``cse7766:`` block carries
    ``current`` / ``voltage`` / ``power`` sub-blocks each with a
    ``Smart Plug <Quantity>`` name. A query for ``cse7766`` should
    hit the platform line itself, and a query for the platform's
    accuracy_decimals should hit each sub-block. Pin both: line
    numbers must be 1-based and ascending so the frontend's
    ``?line=<n>`` jump lands on the right line.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = _seed_fleet(tmp_path)

    results = await controller.search_yaml(query="accuracy_decimals")

    assert len(results) == 1
    assert results[0]["configuration"] == "smart_plug.yaml"
    line_numbers = [m["line_number"] for m in results[0]["matches"]]
    assert line_numbers == sorted(line_numbers)
    assert all(n >= 1 for n in line_numbers)
    # cse7766 has three quantities (current/voltage/power), each
    # with an accuracy_decimals — all three rows should be present.
    assert len(line_numbers) == 3


# ---------------------------------------------------------------------------
# Comments + substitutions are searchable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_text_is_searchable(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """Searching matches text inside YAML comments, not just keys.

    The bluetooth-proxy fixture's ``esp32_ble_tracker.scan_parameters``
    block carries a ``# We currently use the defaults ...`` comment
    — a user grepping for "coexistence" expects to land on the
    comment line, not silently miss it because it's not a key.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = _seed_fleet(tmp_path)

    results = await controller.search_yaml(query="coexistence")

    assert len(results) == 1
    assert results[0]["configuration"] == "bluetooth_proxy.yaml"
    # Match should be the actual comment line.
    assert "coexistence" in results[0]["matches"][0]["line_text"]


@pytest.mark.asyncio
async def test_substitution_interpolation_matches_as_literal_text(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``${name}`` substitution syntax is literal text to the searcher.

    The packaged-device fixture carries ``name: ${name}`` and
    ``friendly_name: ${friendly_name}`` because it pulls its real
    config from a remote package. Searching for ``${name}`` should
    find both of those interpolation sites — important because
    that's exactly how a user debugs a substitution issue.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = _seed_fleet(tmp_path)

    results = await controller.search_yaml(query="${name}")

    assert len(results) == 1
    assert results[0]["configuration"] == "packaged_device.yaml"
    assert len(results[0]["matches"]) >= 1


@pytest.mark.asyncio
async def test_secret_reference_is_searchable(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """``!secret`` references match across every fixture that uses them.

    Every fixture in the fleet pulls ``wifi_password`` from
    ``!secret`` (the proxy / plug / packaged shape all do).
    Pin that the search treats ``!secret`` as ordinary text and
    surfaces every device that references the named secret —
    useful for "which devices use this secret?" auditing.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = _seed_fleet(tmp_path)

    # Bump max_results well above the fixture count so the fleet-wide
    # secret hit isn't truncated.
    results = await controller.search_yaml(query="!secret wifi_password", max_results=50)

    matched = {hit["configuration"] for hit in results}
    assert matched == {
        "bluetooth_proxy.yaml",
        "smart_plug.yaml",
        "packaged_device.yaml",
    }


# ---------------------------------------------------------------------------
# Caps still hold against realistic content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_file_cap_holds_against_chatty_fixture(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A query that hits many lines in one fixture still respects the cap.

    The smart-plug fixture has ~10 ``Smart Plug ...`` name lines
    across its sensor / switch / binary_sensor blocks. A naive
    search would return all of them, drowning out hits in the
    other devices. Pin that the per-file cap (5) holds even on a
    realistic, long-fixture query.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = _seed_fleet(tmp_path)

    results = await controller.search_yaml(query="Smart Plug")

    plug_hits = [hit for hit in results if hit["configuration"] == "smart_plug.yaml"]
    assert len(plug_hits) == 1
    assert len(plug_hits[0]["matches"]) == 5


@pytest.mark.asyncio
async def test_yaml_directive_marker_does_not_break_match(
    tmp_path: Path,
    make_controller: MakeControllerFactory,
) -> None:
    """A ``---`` document-start marker in a fixture doesn't shadow other matches.

    The ethernet fixture starts with the YAML directive marker
    ``---`` on line 1. Pin that search ignores it as a special
    marker — i.e. a query for ``esphome:`` still hits line 2,
    not line 1.
    """
    controller = make_controller(tmp_path)
    controller._scanner.devices = _seed_fleet(tmp_path)

    results = await controller.search_yaml(query="esphome:")

    eth_hit = next(hit for hit in results if hit["configuration"] == "ethernet_proxy.yaml")
    line_numbers = [m["line_number"] for m in eth_hit["matches"]]
    # Top-level ``esphome:`` block opens on line 2 (line 1 is ``---``).
    assert 2 in line_numbers
