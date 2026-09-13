"""Pin the schema-driven component-alias fold and the shipped catalog's alias-free state."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import esphome_device_builder
from esphome_device_builder.models.boards import Platform

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import sync_components  # noqa: E402

_DEFINITIONS = Path(esphome_device_builder.__file__).parent / "definitions"

_ALIASES = {"esp32_improv": "improv_ble", "rp2040": "rp2"}


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


def _section(*, alias_of: str | None = None, trigger: str | None = None) -> dict:
    config_vars: dict = {"authorizer": {"key": "Required", "type": "string"}}
    if trigger:
        config_vars[trigger] = {"key": "Optional", "type": "trigger"}
    section: dict = {
        "schemas": {"CONFIG_SCHEMA": {"type": "schema", "schema": {"config_vars": config_vars}}}
    }
    if alias_of:
        section["alias_of"] = alias_of
        section["removal_version"] = "2027.4.0"
    return section


def _write_schema_dir(tmp_path: Path, *, trigger: str | None = None) -> Path:
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    (schema_dir / "improv_ble.json").write_text(
        json.dumps({"improv_ble": _section(trigger=trigger)}), encoding="utf-8"
    )
    (schema_dir / "esp32_improv.json").write_text(
        json.dumps({"esp32_improv": _section(alias_of="improv_ble", trigger=trigger)}),
        encoding="utf-8",
    )
    (schema_dir / "rp2.json").write_text(json.dumps({"rp2": _section()}), encoding="utf-8")
    (schema_dir / "rp2040.json").write_text(
        json.dumps({"rp2040": _section(alias_of="rp2")}), encoding="utf-8"
    )
    (schema_dir / "broken.json").write_text("{not json", encoding="utf-8")
    return schema_dir


def test_collect_schema_aliases_reads_alias_of(tmp_path: Path) -> None:
    schema_dir = _write_schema_dir(tmp_path)
    assert sync_components._collect_schema_aliases(schema_dir) == _ALIASES


def test_build_entries_skips_an_alias_section(tmp_path: Path) -> None:
    schema_dir = _write_schema_dir(tmp_path)
    entries = sync_components.build_entries_from_file(
        schema_dir / "esp32_improv.json", MagicMock(), schema_dir, {}
    )
    assert entries == []


def test_fold_drops_alias_entries() -> None:
    entries = [
        _entry("improv_ble", category="core"),
        _entry("esp32_improv"),
        _entry("rp2", category="core"),
        _entry("rp2040"),
    ]
    sync_components._fold_component_aliases(entries, _ALIASES)
    assert [e["id"] for e in entries] == ["improv_ble", "rp2"]


def test_fold_rewrites_dependencies() -> None:
    entries = [
        _entry("rp2040_ble", dependencies=["logger", "rp2040"]),
        _entry("output.rp2040_pwm", dependencies=["rp2040", "rp2"]),
        _entry("binary_sensor.gpio", dependencies=["esp32_improv"]),
        _entry("sensor.dht", dependencies=["esp32"]),
    ]
    sync_components._fold_component_aliases(entries, _ALIASES)
    assert entries[0]["dependencies"] == ["logger", "rp2"]
    assert entries[1]["dependencies"] == ["rp2"]
    assert entries[2]["dependencies"] == ["improv_ble"]
    assert entries[3]["dependencies"] == ["esp32"]


def test_fold_noop_without_aliases() -> None:
    entries = [_entry("rp2040", dependencies=["rp2040"]), _entry("esp32", category="core")]
    sync_components._fold_component_aliases(entries, {})
    assert [e["id"] for e in entries] == ["rp2040", "esp32"]
    assert entries[0]["dependencies"] == ["rp2040"]


def test_build_automations_skips_alias_sections(tmp_path: Path) -> None:
    schema_dir = _write_schema_dir(tmp_path, trigger="on_state")
    catalog = sync_components.build_automations(
        schema_dir=schema_dir, component_ids={"improv_ble", "rp2"}
    )
    assert [t["id"] for t in catalog["triggers"]] == ["improv_ble.on_state"]
    assert catalog["triggers"][0]["applies_to"] == ["improv_ble"]


def test_shipped_index_has_no_rp2040_alias() -> None:
    index = json.loads((_DEFINITIONS / "components.index.json").read_text())
    ids = {entry["id"] for entry in index["components"]}
    assert "rp2040" not in ids
    assert "rp2" in ids
    for entry in index["components"]:
        assert "rp2040" not in (entry.get("dependencies") or [])


def test_shipped_catalog_carries_no_installed_esphome_alias() -> None:
    loader = pytest.importorskip("esphome.loader")
    aliases = set(loader.get_alias_metadata())
    assert aliases
    index = json.loads((_DEFINITIONS / "components.index.json").read_text())
    assert not aliases & {entry["id"] for entry in index["components"]}
    automations = json.loads((_DEFINITIONS / "automations.index.json").read_text())
    automation_ids = {
        entry["id"]
        for entries in automations.values()
        if isinstance(entries, list)
        for entry in entries
    }
    assert not {aid for aid in automation_ids if aid.split(".", 1)[0] in aliases}


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
