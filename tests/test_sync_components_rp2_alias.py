"""Pin the ``rp2040`` -> ``rp2`` component-alias fold and the shipped catalog's alias-free state."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import esphome_device_builder
from esphome_device_builder.models.boards import Platform

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import sync_components  # noqa: E402

_DEFINITIONS = Path(esphome_device_builder.__file__).parent / "definitions"


def test_platform_enum_accepts_both_rp2_spellings() -> None:
    assert Platform("rp2") is Platform.RP2
    assert Platform("rp2040") is Platform.RP2


def test_platform_enum_still_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="rp3"):
        Platform("rp3")


def _entry(component_id: str, **overrides: object) -> dict:
    entry = {
        "id": component_id,
        "name": component_id,
        "category": "misc",
        "config_entries": [],
        "dependencies": None,
        "image_url": None,
    }
    entry.update(overrides)
    return entry


def test_fold_drops_the_alias_and_keeps_the_rich_rp2_body() -> None:
    rich = _entry(
        "rp2",
        name="RP2 Platform",
        category="core",
        description="Covers RP2040 and RP2350.",
        docs_url="https://esphome.io/components/rp2",
        image_url="https://esphome.io/images/rp2.svg",
        config_entries=[{"key": "board"}, {"key": "variant"}],
    )
    legacy = _entry(
        "rp2040",
        name="RP2040 Platform",
        description="Sparse alias copy.",
        config_entries=[{"key": "variant"}],
    )
    entries = [rich, legacy]

    sync_components._fold_rp2_component_alias(entries)

    assert [e["id"] for e in entries] == ["rp2"]
    folded = entries[0]
    assert folded["name"] == "RP2 Platform"
    assert folded["category"] == "core"
    assert folded["image_url"] == "https://esphome.io/images/rp2.svg"
    assert folded["description"] == "Covers RP2040 and RP2350."
    assert folded["docs_url"] == "https://esphome.io/components/rp2"
    assert [e["key"] for e in folded["config_entries"]] == ["board", "variant"]


def test_fold_rekeys_a_lone_rp2040_entry() -> None:
    entries = [_entry("rp2040", name="RP2040 Platform")]
    sync_components._fold_rp2_component_alias(entries)
    assert [e["id"] for e in entries] == ["rp2"]
    assert entries[0]["name"] == "RP2040 Platform"


def test_fold_borrows_identity_fields_the_rich_entry_lacks() -> None:
    entries = [
        _entry("rp2", category="core", image_url="", config_entries=[{"key": "variant"}]),
        _entry("rp2040", image_url="https://esphome.io/images/rp2040.svg", category="misc"),
    ]
    sync_components._fold_rp2_component_alias(entries)
    assert [e["id"] for e in entries] == ["rp2"]
    assert entries[0]["image_url"] == "https://esphome.io/images/rp2040.svg"
    assert entries[0]["category"] == "core"
    assert [e["key"] for e in entries[0]["config_entries"]] == ["variant"]


def test_fold_rewrites_dependencies() -> None:
    entries = [
        _entry("rp2040_ble", dependencies=["logger", "rp2040"]),
        _entry("output.rp2040_pwm", dependencies=["rp2040", "rp2"]),
        _entry("sensor.dht", dependencies=["esp32"]),
    ]
    sync_components._fold_rp2_component_alias(entries)
    assert entries[0]["dependencies"] == ["logger", "rp2"]
    assert entries[1]["dependencies"] == ["rp2"]
    assert entries[2]["dependencies"] == ["esp32"]


def test_fold_noop_without_rp2040() -> None:
    entries = [_entry("rp2", category="core"), _entry("esp32", category="core")]
    sync_components._fold_rp2_component_alias(entries)
    assert [e["id"] for e in entries] == ["rp2", "esp32"]


def test_shipped_index_has_no_rp2040_alias() -> None:
    index = json.loads((_DEFINITIONS / "components.index.json").read_text())
    ids = {entry["id"] for entry in index["components"]}
    assert "rp2040" not in ids
    assert "rp2" in ids
    for entry in index["components"]:
        assert "rp2040" not in (entry.get("dependencies") or [])


def test_shipped_rp2_body_is_the_real_schema() -> None:
    body = json.loads((_DEFINITIONS / "components" / "rp2.json").read_text())
    entries = {entry["key"]: entry for entry in body["config_entries"]}
    # Variant-driven platform: ``board`` stays stripped (_DEPRECATED_FIELDS);
    # the board catalog supplies it.
    assert "board" not in entries
    # Value-level: the docs-repaired schema, not the sparse alias shell.
    variant = entries["variant"]
    assert "rp2350" in variant["description"]
    assert variant.get("help_link")
