"""Pin the component-alias skip, dependency respell, and alias-free shipped catalog."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import esphome_device_builder

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import sync_components  # noqa: E402

_DEFINITIONS = Path(esphome_device_builder.__file__).parent / "definitions"


def _section(*, alias_of: str | None = None) -> dict:
    config_vars = {"on_state": {"key": "Optional", "type": "trigger"}}
    section: dict = {
        "schemas": {"CONFIG_SCHEMA": {"type": "schema", "schema": {"config_vars": config_vars}}}
    }
    if alias_of:
        section["alias_of"] = alias_of
    return section


def _write_schema_dir(tmp_path: Path) -> Path:
    schema_dir = tmp_path / "schema"
    schema_dir.mkdir()
    (schema_dir / "improv_ble.json").write_text(json.dumps({"improv_ble": _section()}))
    (schema_dir / "esp32_improv.json").write_text(
        json.dumps({"esp32_improv": _section(alias_of="improv_ble")})
    )
    return schema_dir


def test_component_aliases_mirror_the_installed_loader() -> None:
    loader = sync_components._get_esphome_loader()
    expected = {legacy: meta.canonical for legacy, meta in loader.get_alias_metadata().items()}
    assert sync_components._component_aliases() == expected
    assert expected["rp2040"] == "rp2"


def test_build_entries_skips_an_alias_section(tmp_path: Path) -> None:
    schema_dir = _write_schema_dir(tmp_path)
    entries = sync_components.build_entries_from_file(
        schema_dir / "esp32_improv.json", MagicMock(), schema_dir, {}
    )
    assert entries == []


def test_build_automations_skips_alias_sections(tmp_path: Path) -> None:
    schema_dir = _write_schema_dir(tmp_path)
    catalog = sync_components.build_automations(schema_dir=schema_dir, component_ids={"improv_ble"})
    assert [t["id"] for t in catalog["triggers"]] == ["improv_ble.on_state"]
    assert catalog["triggers"][0]["applies_to"] == ["improv_ble"]


def test_respell_alias_dependencies() -> None:
    entries = [
        {"id": "rp2040_ble", "dependencies": ["logger", "rp2040"]},
        {"id": "output.rp2040_pwm", "dependencies": ["rp2040", "rp2"]},
        {"id": "binary_sensor.gpio", "dependencies": ["esp32_improv"]},
        {"id": "sensor.dht", "dependencies": ["esp32"]},
        {"id": "wifi", "dependencies": None},
    ]
    sync_components._respell_alias_dependencies(
        entries, {"esp32_improv": "improv_ble", "rp2040": "rp2"}
    )
    assert [e["dependencies"] for e in entries] == [
        ["logger", "rp2"],
        ["rp2"],
        ["improv_ble"],
        ["esp32"],
        None,
    ]


def test_shipped_catalog_carries_no_installed_esphome_alias() -> None:
    aliases = set(sync_components._component_aliases())
    assert aliases
    index = json.loads((_DEFINITIONS / "components.index.json").read_text())
    for entry in index["components"]:
        assert entry["id"] not in aliases
        assert not aliases & set(entry.get("dependencies") or [])
    automations = json.loads((_DEFINITIONS / "automations.index.json").read_text())
    for entries in automations.values():
        if isinstance(entries, list):
            for entry in entries:
                assert entry["id"].split(".", 1)[0] not in aliases


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
