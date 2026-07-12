"""``api.actions`` / ``services`` stay hidden — the automations UI owns them."""

from __future__ import annotations

import json
from pathlib import Path

from script.sync_components import (  # type: ignore[import-not-found]
    _FIELD_OVERRIDES,
)

_BODIES_DIR = (
    Path(__file__).resolve().parent.parent / "esphome_device_builder" / "definitions" / "components"
)


def _api_entries() -> dict[str, dict]:
    body = json.loads((_BODIES_DIR / "api.json").read_text(encoding="utf-8"))
    return {entry["key"]: entry for entry in body["config_entries"]}


def test_override_hides_both_keys() -> None:
    assert _FIELD_OVERRIDES[("api", "actions")]["hidden"] is True
    assert _FIELD_OVERRIDES[("api", "services")]["hidden"] is True


def test_committed_body_pins_actions_hidden() -> None:
    entries = _api_entries()
    assert entries["actions"]["hidden"] is True
    assert entries["services"]["hidden"] is True
