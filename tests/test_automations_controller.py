"""Tests for ``controllers/automations.py`` — context-aware triggers / actions.

The module pre-defines two static catalogues — device-level
triggers (always available), component-level triggers (gated on
the device's configured platform types), plus a flat list of
actions — and exposes three WS commands that slice them:

* ``automations/get_triggers`` — full list, optionally narrowed
  by a single ``platform_type`` filter.
* ``automations/get_actions`` — every action in the catalogue.
* ``automations/get_available`` — reads the device's YAML, scans
  for top-level keys matching the platform-type allowlist, and
  returns the union of device-level triggers + every
  component-level trigger whose ``platform_types`` intersects
  the present set.

The pin points for these tests are the slicing rules — each
``platform_types`` arm needs to be exercised so a regression that
swaps ``any`` for ``all`` (a real trap given the trigger list
mixes shared platform_types like ``binary_sensor`` + ``button``)
shows up immediately.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from esphome_device_builder.controllers.automations import (
    _ACTIONS,
    _COMPONENT_TRIGGERS,
    _DEVICE_TRIGGERS,
    AutomationsController,
)


def _make_controller(config_dir: Path) -> AutomationsController:
    """Build an ``AutomationsController`` with a stub ``DeviceBuilder``.

    ``__init__`` only stashes the device_builder reference; the
    only attribute the controller actually reaches for is
    ``self._db.settings.rel_path``, used by
    ``get_available_for_device`` to resolve the configuration
    filename to an on-disk Path. Wiring ``rel_path`` to
    ``config_dir.joinpath`` mirrors what production does and
    keeps the test free of Settings construction.
    """
    db = MagicMock()
    db.settings.rel_path = config_dir.joinpath
    return AutomationsController(db)


# ---------------------------------------------------------------------------
# get_triggers
# ---------------------------------------------------------------------------


async def test_get_triggers_without_filter_returns_full_catalogue() -> None:
    """No ``platform_type`` keyword → device + component triggers, in order.

    The unfiltered call returns everything; pin both halves of
    the catalogue land in the response so a regression that
    accidentally switched the no-arg branch to "device only" or
    "component only" surfaces here.
    """
    controller = _make_controller(Path("/unused"))
    result = await controller.get_triggers()

    ids = [t["id"] for t in result]
    assert len(result) == len(_DEVICE_TRIGGERS) + len(_COMPONENT_TRIGGERS)
    assert ids[: len(_DEVICE_TRIGGERS)] == [t.id for t in _DEVICE_TRIGGERS]
    # Every component-level id is also present.
    assert {t.id for t in _COMPONENT_TRIGGERS} <= set(ids)


async def test_get_triggers_filters_by_platform_type() -> None:
    """``platform_type="sensor"`` keeps device-level + sensor-only triggers.

    Device-level triggers carry ``platform_types=[]`` (the empty
    list is the "always-applicable" sentinel) so they pass the
    ``not t.platform_types`` half of the predicate. Component-
    level triggers pass when their ``platform_types`` list
    contains the requested type. Pin both arms by asserting (a)
    every device-level trigger is present, (b) ``on_value`` (a
    sensor-only trigger) is present, (c) ``on_press`` (button /
    binary_sensor only — no sensor) is absent.
    """
    controller = _make_controller(Path("/unused"))
    result = await controller.get_triggers(platform_type="sensor")
    ids = {t["id"] for t in result}

    assert {t.id for t in _DEVICE_TRIGGERS} <= ids
    assert "on_value" in ids
    assert "on_press" not in ids


async def test_get_triggers_filter_excludes_unrelated_component_types() -> None:
    """Filtering by ``light`` drops binary_sensor-only triggers.

    Pin the negative case: ``on_release`` is binary_sensor-only,
    while ``on_turn_on`` lists ``light`` among its platform_types.
    A regression that flipped the membership check (``not in``
    vs ``in``) would invert these and pass other less-specific
    tests, so call out both sides.
    """
    controller = _make_controller(Path("/unused"))
    result = await controller.get_triggers(platform_type="light")
    ids = {t["id"] for t in result}

    assert "on_turn_on" in ids
    assert "on_turn_off" in ids
    assert "on_release" not in ids
    assert "on_value" not in ids


# ---------------------------------------------------------------------------
# get_actions
# ---------------------------------------------------------------------------


async def test_get_actions_returns_full_catalogue() -> None:
    """Every entry in ``_ACTIONS`` is serialised and returned.

    No filtering on actions today — pin the full dump so a
    refactor that introduces conditional gating (e.g. "hide
    light.* when no light component is configured") needs to
    update this test deliberately.
    """
    controller = _make_controller(Path("/unused"))
    result = await controller.get_actions()

    assert [a["id"] for a in result] == [a.id for a in _ACTIONS]


# ---------------------------------------------------------------------------
# get_available_for_device
# ---------------------------------------------------------------------------


async def test_get_available_for_device_includes_only_present_platform_types(
    tmp_path: Path,
) -> None:
    """A device with ``binary_sensor:`` configured surfaces button-style triggers.

    The scan walks the YAML line-by-line looking for non-indented
    lines containing ``:``, then intersects the extracted key with
    the controller's platform-type allowlist. A device with only
    ``binary_sensor:`` should see ``on_press`` / ``on_release`` /
    ``on_click`` / ``on_state`` but not ``on_value`` (which is
    sensor-specific).
    """
    config = tmp_path / "kitchen.yaml"
    config.write_text(
        "esphome:\n  name: kitchen\nbinary_sensor:\n  - platform: gpio\n    pin: GPIO0\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_available_for_device(configuration="kitchen.yaml")

    assert result["present_platform_types"] == ["binary_sensor"]
    trigger_ids = {t["id"] for t in result["triggers"]}
    assert {t.id for t in _DEVICE_TRIGGERS} <= trigger_ids
    assert "on_press" in trigger_ids
    assert "on_release" in trigger_ids
    assert "on_value" not in trigger_ids
    # Actions are unconditional.
    assert [a["id"] for a in result["actions"]] == [a.id for a in _ACTIONS]


async def test_get_available_for_device_with_no_components_returns_only_device_triggers(
    tmp_path: Path,
) -> None:
    """A device YAML carrying no platform-type blocks yields only device-level triggers.

    Component-level triggers all require *some* platform_type to
    be present (their list is non-empty); a config with just
    ``esphome:`` and ``wifi:`` (neither in the allowlist) leaves
    the present-set empty and the ``any(...)`` predicate folds
    every component-level trigger out.
    """
    config = tmp_path / "minimal.yaml"
    config.write_text("esphome:\n  name: m\nwifi:\n  ssid: x\n", encoding="utf-8")
    controller = _make_controller(tmp_path)

    result = await controller.get_available_for_device(configuration="minimal.yaml")

    assert result["present_platform_types"] == []
    assert [t["id"] for t in result["triggers"]] == [t.id for t in _DEVICE_TRIGGERS]


async def test_get_available_for_device_unions_triggers_across_multiple_components(
    tmp_path: Path,
) -> None:
    """``binary_sensor:`` plus ``sensor:`` returns the union of both arms.

    The component-level trigger filter uses ``any(pt in
    present_types for pt in trigger.platform_types)`` — a
    trigger that lists multiple platform_types lights up as
    soon as *any* is present. Pin that ``on_state`` (which
    carries both ``binary_sensor`` and ``sensor``) appears
    once, not twice.
    """
    config = tmp_path / "mix.yaml"
    config.write_text(
        "esphome:\n  name: mix\n"
        "binary_sensor:\n  - platform: gpio\n    pin: GPIO0\n"
        "sensor:\n  - platform: dht\n    pin: GPIO4\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_available_for_device(configuration="mix.yaml")

    assert result["present_platform_types"] == ["binary_sensor", "sensor"]
    trigger_ids = [t["id"] for t in result["triggers"]]
    assert "on_press" in trigger_ids
    assert "on_value" in trigger_ids
    # ``on_state`` lists both — pin that the union doesn't
    # double-count it. The component-level loop iterates the
    # static list once per trigger, so de-duplication is
    # implicit; pin it so a refactor that switches to "for each
    # platform_type, append matching triggers" doesn't slip in
    # silent duplicates.
    assert trigger_ids.count("on_state") == 1


async def test_get_available_for_device_ignores_indented_keys(tmp_path: Path) -> None:
    """Only column-zero keys count — indented ``sensor:`` under another block is skipped.

    The scan key-detection rule is "first character is non-
    whitespace and the line contains a colon". A nested ``-
    sensor:`` (the platform of a list item) is indented and
    must not register as a top-level platform-type block —
    otherwise every sensor-only trigger leaks into a config
    with no real ``sensor:`` block.
    """
    config = tmp_path / "edge.yaml"
    # ``binary_sensor:`` exists at the top level; the indented
    # ``    sensor:`` key under the list item is decorative and
    # must NOT register the device as having a sensor block.
    config.write_text(
        "esphome:\n  name: edge\n"
        "binary_sensor:\n"
        "  - platform: gpio\n"
        "    pin: GPIO0\n"
        "    sensor: not-a-real-key\n",
        encoding="utf-8",
    )
    controller = _make_controller(tmp_path)

    result = await controller.get_available_for_device(configuration="edge.yaml")

    assert result["present_platform_types"] == ["binary_sensor"]
    assert "on_value" not in {t["id"] for t in result["triggers"]}


@pytest.mark.parametrize(
    "line",
    [
        "# binary_sensor: in a comment\n",
        "binary_sensor without colon\n",
    ],
    ids=["comment-line", "no-colon"],
)
async def test_get_available_for_device_skips_lines_without_colon(
    tmp_path: Path,
    line: str,
) -> None:
    """Lines without a ``:`` (or whose ``:`` is inside a comment) don't match.

    The ``":" in line`` check is the second half of the
    top-level-key heuristic. A YAML comment that mentions a
    platform-type name and a stray non-key line both fail the
    membership test and must not seed the present-set. Pin both
    so a regression that drops the colon check (or moves it)
    surfaces here.
    """
    config = tmp_path / "edge.yaml"
    config.write_text(f"esphome:\n  name: edge\n{line}", encoding="utf-8")
    controller = _make_controller(tmp_path)

    result = await controller.get_available_for_device(configuration="edge.yaml")

    assert result["present_platform_types"] == []
