"""Tests for ``_emit_split_automations_catalog``'s defensive guards."""

from __future__ import annotations

import pytest

import script.sync_components as sync_module
from script.sync_components import (  # type: ignore[import-not-found]
    _emit_split_automations_catalog,
)


def _minimal_trigger(**overrides) -> dict:
    body = {
        "id": "on_boot",
        "name": "On Boot",
        "description": "Fires once at startup.",
        "docs_url": "https://example/on_boot",
        "applies_to": [],
        "is_device_level": True,
        "config_entries": [],
    }
    body.update(overrides)
    return body


def test_emit_split_automations_writes_index_and_per_type_bodies(
    tmp_path,
    monkeypatch,
) -> None:
    """Happy path: slim index + per-type body files land."""
    monkeypatch.setattr(sync_module, "_AUTOMATIONS_BODIES_DIR", tmp_path / "automations")
    monkeypatch.setattr(sync_module, "_AUTOMATIONS_INDEX_FILE", tmp_path / "automations.index.json")

    automations = {
        "triggers": [_minimal_trigger(id="on_boot")],
        "actions": [],
        "conditions": [],
        "light_effects": [],
        "filters": [],
    }
    _emit_split_automations_catalog(automations, "2026.5.1")

    assert (tmp_path / "automations.index.json").is_file()
    assert (tmp_path / "automations" / "triggers" / "on_boot.json").is_file()
    # Each subcatalog gets its own subdir even when empty.
    for sub in ("triggers", "actions", "conditions", "light_effects", "filters"):
        assert (tmp_path / "automations" / sub).is_dir()


def test_emit_split_automations_refuses_body_that_fails_roundtrip(
    tmp_path,
    monkeypatch,
) -> None:
    """Mashumaro-strict roundtrip is required before a body lands on disk."""
    monkeypatch.setattr(sync_module, "_AUTOMATIONS_BODIES_DIR", tmp_path / "automations")
    monkeypatch.setattr(sync_module, "_AUTOMATIONS_INDEX_FILE", tmp_path / "automations.index.json")

    automations = {
        # Missing required ``description`` field — AutomationTrigger.from_dict
        # rejects this and the guard turns it into ValueError.
        "triggers": [
            {
                "id": "on_broken",
                "name": "Broken",
                "docs_url": "",
                "applies_to": [],
                "is_device_level": False,
                "config_entries": [],
            }
        ],
        "actions": [],
        "conditions": [],
        "light_effects": [],
        "filters": [],
    }

    with pytest.raises(ValueError, match="fails roundtrip"):
        _emit_split_automations_catalog(automations, "2026.5.1")


@pytest.mark.parametrize(
    "bad_id",
    [
        "../escape",
        "subdir/escape",
        "back\\slash",
        "null\x00byte",
        "",
    ],
)
def test_emit_split_automations_refuses_traversal_shaped_id(
    bad_id: str,
    tmp_path,
    monkeypatch,
) -> None:
    """Mirror of the runtime ``is_unsafe_catalog_id`` guard for the build side."""
    monkeypatch.setattr(sync_module, "_AUTOMATIONS_BODIES_DIR", tmp_path / "automations")
    monkeypatch.setattr(sync_module, "_AUTOMATIONS_INDEX_FILE", tmp_path / "automations.index.json")

    automations = {
        "triggers": [_minimal_trigger(id=bad_id)],
        "actions": [],
        "conditions": [],
        "light_effects": [],
        "filters": [],
    }

    with pytest.raises(ValueError, match="traversal-shaped"):
        _emit_split_automations_catalog(automations, "2026.5.1")
