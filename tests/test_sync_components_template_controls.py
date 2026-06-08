"""Tests for ``_promote_template_controls`` in ``script/sync_components.py``.

A ``*.template`` entity is non-functional without its control fields
(``optimistic`` / the ``*_action`` handlers), but ESPHome marks them
optional, so the catalog generator would otherwise hide them under
"Show advanced" (#1324). Pin the promotion and its scope so a future
sync-script edit can't regress them back to advanced, or over-promote a
non-template component's optional ``*_action`` handlers.
"""

from __future__ import annotations

from pathlib import Path

import orjson

from script.sync_components import _promote_template_controls  # type: ignore[import-not-found]

_COMPONENTS_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)


def _advanced_by_key(component_id: str) -> dict[str, bool]:
    body = orjson.loads((_COMPONENTS_DIR / f"{component_id}.json").read_bytes())
    return {e["key"]: bool(e.get("advanced", False)) for e in body["config_entries"]}


def test_promote_marks_optimistic_and_action_fields_core() -> None:
    """On a ``.template`` id, ``optimistic`` and ``*_action`` keys flip to non-advanced."""
    entries = [
        {"key": "optimistic", "advanced": True},
        {"key": "turn_on_action", "advanced": True},
        {"key": "set_action", "advanced": True},
        {"key": "id", "advanced": True},
        {"key": "name"},
    ]
    _promote_template_controls("switch.template", entries)
    assert entries[0]["advanced"] is False
    assert entries[1]["advanced"] is False
    assert entries[2]["advanced"] is False
    assert entries[3]["advanced"] is True  # id stays advanced
    assert "advanced" not in entries[4]  # untouched


def test_promote_skips_non_template_components() -> None:
    """A non-``.template`` id keeps its ``optimistic`` / ``*_action`` advanced."""
    entries = [{"key": "optimistic", "advanced": True}, {"key": "turn_on_action", "advanced": True}]
    _promote_template_controls("switch.hbridge", entries)
    assert entries[0]["advanced"] is True
    assert entries[1]["advanced"] is True


def test_template_switch_control_fields_are_core_in_catalog() -> None:
    """The generated ``switch.template`` body surfaces the control fields on the main form."""
    adv = _advanced_by_key("switch.template")
    assert adv["optimistic"] is False
    assert adv["turn_on_action"] is False
    assert adv["turn_off_action"] is False
    assert adv["lambda"] is True  # lambda stays advanced (genuinely advanced C++)


def test_template_promotion_does_not_over_reach() -> None:
    """Non-template ``*_action`` carriers and non-template ``optimistic`` stay advanced."""
    assert _advanced_by_key("climate.thermostat")["fan_mode_high_action"] is True
    assert _advanced_by_key("switch.hbridge")["optimistic"] is True
