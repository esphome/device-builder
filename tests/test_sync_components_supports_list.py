"""Tests for the deterministic ``supports_list`` trigger flag.

ESPHome's ``automation.validate_automation(single=)`` decides whether a
trigger accepts a list of multiple handlers; the schema bundle drops that
flag, so ``sync_components`` recovers it from the live validator closure
(``supports_list = not single``). These tests pin the closure introspection,
a curated subset of known triggers in the shipped catalog, and a
catalog-vs-live cross-check so the emitter and resolver can't silently drift.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import voluptuous as vol

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import sync_components  # noqa: E402
from esphome import automation  # noqa: E402

from esphome_device_builder.controllers.automations import catalog  # noqa: E402

# Known single=False triggers -> a YAML list of handlers is valid.
_LIST_CAPABLE = (
    "on_boot",
    "on_loop",
    "on_shutdown",
    "time.on_time",
    "binary_sensor.on_press",
    "binary_sensor.on_state",
    "mqtt.on_connect",
)
# Known single=True triggers -> exactly one handler; never list-edited.
_SINGLE_ONLY = (
    "touchscreen.on_touch",
    "touchscreen.on_release",
    "wifi.on_connect",
    "wifi.on_disconnect",
    "http_request.on_response",
    "ethernet.on_connect",
)


def test_validate_automation_single_reads_closure() -> None:
    """The ``single`` flag is read off the ``validate_automation`` closure."""
    read = sync_components._validate_automation_single
    assert read(automation.validate_automation(single=True)) is True
    assert read(automation.validate_automation()) is False
    assert read(lambda v: v) is None


def test_single_deep_descends_compound_validators() -> None:
    """``_single_deep`` recovers ``single`` through a ``vol.All`` wrapper."""
    wrapped = vol.All(automation.validate_automation(single=True), lambda v: v)
    assert sync_components._single_deep(wrapped) is True
    assert sync_components._single_deep(lambda v: v) is None


def test_live_trigger_singles_core_and_component() -> None:
    """Live introspection finds device-level and component triggers.

    A loud failure here means the closure-introspection mechanism broke on the
    installed esphome (e.g. the freevar was renamed), not a catalog drift.
    """
    esphome_singles = dict(sync_components._live_trigger_singles("esphome"))
    assert esphome_singles.get("on_boot") is False
    assert esphome_singles.get("on_loop") is False
    assert esphome_singles.get("on_shutdown") is False

    bs = dict(sync_components._live_trigger_singles("binary_sensor"))
    assert bs.get("on_press") is False

    ts = dict(sync_components._live_trigger_singles("touchscreen"))
    assert ts.get("on_touch") is True

    assert sync_components._live_trigger_singles("no_such_component_zzz") == frozenset()


@pytest.mark.parametrize("trigger_id", _LIST_CAPABLE)
def test_shipped_catalog_marks_known_list_capable(trigger_id: str) -> None:
    """Known single=False triggers ship with ``supports_list`` True."""
    assert catalog.trigger_supports_list(trigger_id) is True


@pytest.mark.parametrize("trigger_id", _SINGLE_ONLY)
def test_shipped_catalog_marks_known_single_only(trigger_id: str) -> None:
    """Known single=True triggers ship with ``supports_list`` False."""
    assert catalog.trigger_supports_list(trigger_id) is False


def test_repeatable_field_is_gone() -> None:
    """``repeatable`` was replaced by ``supports_list``; no entry carries it."""
    assert all(not hasattr(t, "repeatable") for t in catalog.all_triggers())


def test_device_trigger_ids_match_is_device_level() -> None:
    """``device_trigger_ids`` is derived from the catalog, not a hand list."""
    assert set(catalog.device_trigger_ids()) == {
        t.id for t in catalog.all_triggers() if t.is_device_level
    }


def test_shipped_catalog_matches_live_introspection() -> None:
    """Every shipped trigger's ``supports_list`` equals the live ``single is False``.

    Deterministic guard: recompute the flag from the same resolver the sync uses
    and assert equality, so the emitter and resolver never diverge and the
    committed catalog can't go stale against the installed esphome.
    """
    mismatches: list[tuple[str, bool, bool | None]] = []
    for trigger in catalog.all_triggers():
        if trigger.is_device_level:
            top_key, key = "esphome", trigger.id
        else:
            top_key, key = trigger.id.rsplit(".", 1)
        single = dict(sync_components._live_trigger_singles(top_key)).get(key)
        if trigger.supports_list != (single is False):
            mismatches.append((trigger.id, trigger.supports_list, single))
    assert not mismatches, f"supports_list drift: {mismatches[:20]}"
