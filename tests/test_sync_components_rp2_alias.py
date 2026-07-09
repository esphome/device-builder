"""Pin the ``rp2`` -> ``rp2040`` component-alias fold and the shipped catalog's alias-free state."""

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
    assert Platform("rp2") is Platform.RP2040
    assert Platform("rp2040") is Platform.RP2040


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


def test_fold_rekeys_rp2_and_drops_shell() -> None:
    rp2 = _entry(
        "rp2",
        name="RP2 Platform",
        description="Covers RP2040 and RP2350.",
        docs_url="https://esphome.io/components/rp2",
        config_entries=[{"key": "board"}, {"key": "variant"}],
    )
    shell = _entry(
        "rp2040",
        name="RP2040 Platform",
        category="core",
        image_url="https://esphome.io/images/rp2040.svg",
        config_entries=[{"key": "variant"}],
    )
    entries = [rp2, shell]

    sync_components._fold_rp2_component_alias(entries)

    assert [e["id"] for e in entries] == ["rp2040"]
    folded = entries[0]
    assert folded["name"] == "RP2040 Platform"
    assert folded["category"] == "core"
    assert folded["image_url"] == "https://esphome.io/images/rp2040.svg"
    assert folded["description"] == "Covers RP2040 and RP2350."
    assert folded["docs_url"] == "https://esphome.io/components/rp2"
    assert [e["key"] for e in folded["config_entries"]] == ["board", "variant"]


def test_fold_rekeys_rp2_without_shell() -> None:
    entries = [_entry("rp2", name="RP2 Platform")]
    sync_components._fold_rp2_component_alias(entries)
    assert [e["id"] for e in entries] == ["rp2040"]
    assert entries[0]["name"] == "RP2 Platform"


def test_fold_keeps_rp2_fields_over_empty_shell_fields() -> None:
    entries = [
        _entry("rp2", image_url="https://esphome.io/images/rp2.svg"),
        _entry("rp2040", category="core", image_url=""),
    ]
    sync_components._fold_rp2_component_alias(entries)
    assert entries[0]["image_url"] == "https://esphome.io/images/rp2.svg"
    assert entries[0]["category"] == "core"


def test_fold_rewrites_dependencies() -> None:
    entries = [
        _entry("rp2040_ble", dependencies=["logger", "rp2"]),
        _entry("output.rp2040_pwm", dependencies=["rp2", "rp2040"]),
        _entry("sensor.dht", dependencies=["esp32"]),
    ]
    sync_components._fold_rp2_component_alias(entries)
    assert entries[0]["dependencies"] == ["logger", "rp2040"]
    assert entries[1]["dependencies"] == ["rp2040"]
    assert entries[2]["dependencies"] == ["esp32"]


def test_fold_noop_without_rp2() -> None:
    entries = [_entry("rp2040", category="core"), _entry("esp32", category="core")]
    sync_components._fold_rp2_component_alias(entries)
    assert [e["id"] for e in entries] == ["rp2040", "esp32"]


def test_shipped_index_has_no_rp2_alias() -> None:
    index = json.loads((_DEFINITIONS / "components.index.json").read_text())
    ids = {entry["id"] for entry in index["components"]}
    assert "rp2" not in ids
    assert "rp2040" in ids
    for entry in index["components"]:
        assert "rp2" not in (entry.get("dependencies") or [])


def test_shipped_rp2040_body_is_the_real_schema() -> None:
    body = json.loads((_DEFINITIONS / "components" / "rp2040.json").read_text())
    keys = {entry["key"] for entry in body["config_entries"]}
    assert "variant" in keys
    # Variant-driven platform: ``board`` stays stripped (_DEPRECATED_FIELDS);
    # the board catalog supplies it.
    assert "board" not in keys
