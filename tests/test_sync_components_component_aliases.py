"""Pin the component-alias section skip and the catalog alias fold."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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


_ALIASES = {"esp32_improv": "improv_ble", "rp2040": "rp2"}


def _catalog(*deps: list[str] | None) -> list[dict]:
    return [{"id": "improv_ble"}, {"id": "rp2"}] + [
        {"id": f"leaf{i}", "dependencies": d} for i, d in enumerate(deps)
    ]


def test_fold_respells_alias_dependencies() -> None:
    entries = _catalog(["logger", "rp2040"], ["rp2040", "rp2"], ["esp32_improv"], ["esp32"], None)
    sync_components._fold_component_aliases(entries, _ALIASES, check_canonicals=True)
    assert [e.get("dependencies") for e in entries[2:]] == [
        ["logger", "rp2"],
        ["rp2"],
        ["improv_ble"],
        ["esp32"],
        None,
    ]


def test_fold_fails_on_an_untagged_alias_entry() -> None:
    entries = [*_catalog(), {"id": "esp32_improv"}]
    with pytest.raises(SystemExit, match="esp32_improv"):
        sync_components._fold_component_aliases(entries, _ALIASES, check_canonicals=True)


def test_fold_fails_on_a_missing_canonical() -> None:
    entries = [{"id": "rp2"}]
    with pytest.raises(SystemExit, match="improv_ble"):
        sync_components._fold_component_aliases(entries, _ALIASES, check_canonicals=True)


def test_fold_skips_the_canonical_check_on_a_limited_run() -> None:
    entries = [{"id": "rp2"}]
    sync_components._fold_component_aliases(entries, _ALIASES, check_canonicals=False)
    assert entries == [{"id": "rp2"}]


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
