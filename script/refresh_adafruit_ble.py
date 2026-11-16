"""Refresh only manual-entry Adafruit BLE metadata in a copied catalog."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import orjson

from esphome_device_builder.models.automations import AutomationAction, AutomationActionIndex
from esphome_device_builder.models.components import ComponentCatalogEntry
from script import sync_components as sync
from script._catalog_split import dumps_envelope_entries_per_line
from script._esphome_version import assert_installed_esphome

COMPONENT = "ota.adafruit_ble"
ACTION = f"{COMPONENT}.enter_bootloader"


def refresh(definitions: Path, schema_dir: Path, version: str) -> list[str]:
    """Refresh the Adafruit component and entry action, leaving all other entries unchanged."""
    assert_installed_esphome(version, what="refresh_adafruit_ble")
    path = schema_dir / "adafruit_ble.json"
    section = json.loads(path.read_text())["adafruit_ble.ota"] if path.exists() else {}
    fields = (
        section.get("schemas", {}).get("CONFIG_SCHEMA", {}).get("schema", {}).get("config_vars", {})
    )
    supported = "ble_trigger" in fields and "enter_bootloader" in section.get("action", {})
    loader = sync._get_esphome_loader()
    module = loader.get_platform("ota", "adafruit_ble") if loader else None
    from esphome.automation import ACTION_REGISTRY

    live_supported = bool(
        module and hasattr(module.module, "CONF_BLE_TRIGGER") and ACTION in ACTION_REGISTRY
    )
    if supported != live_supported:
        raise ValueError("Adafruit BLE schema and installed core capabilities differ")
    if not supported:
        return []
    assert module is not None
    if list(module.module.DEPENDENCIES) != ["zephyr"]:
        raise ValueError("Unexpected Adafruit BLE dependencies; review the catalog projection")
    component = sync.build_component_entry(
        "adafruit_ble.ota", section, sync.load_index(schema_dir), schema_dir, {}
    )
    assert component is not None
    # These are implicit firmware/framework constraints, not addable YAML hubs.
    component["dependencies"] = ["nrf52"]
    component["supported_platforms"] = ["nrf52"]
    component["provides"] = ["adafruit_ble"]
    component["name"] = "Adafruit BLE Bootloader"
    component["description"] = (
        "Enter the Adafruit BLE bootloader on nRF52; enabling ble_trigger requires "
        "zephyr_ble_server, while disabling it leaves bootloader BLE transfers available."
    )
    for field in component["config_entries"]:
        if field["key"] == "id" and not field.get("description"):
            field["description"] = "Manually specify the ID used for code generation."
    component = ComponentCatalogEntry.from_dict(sync._strip_defaults(component)).to_dict()
    action = sync._convert_automation_action(
        top_key="adafruit_ble.ota",
        domain=COMPONENT,
        wire_prefix=COMPONENT,
        name="enter_bootloader",
        body=section["action"]["enter_bootloader"],
        schema_dir=schema_dir,
    )
    assert action is not None
    for field in action["config_entries"]:
        if field["key"] == "id":
            field["description"] = "The ID of the Adafruit BLE OTA component."
    action = AutomationAction.from_dict(action).to_dict()
    ComponentCatalogEntry.from_dict(component)
    component_index_path = definitions / "components.index.json"
    automation_index_path = definitions / "automations.index.json"
    component_index = json.loads(component_index_path.read_text())
    automation_index = json.loads(automation_index_path.read_text())
    _replace_entry(component_index["components"], sync._strip_index_defaults(component))
    _replace_entry(automation_index["actions"], AutomationActionIndex.from_dict(action).to_dict())
    payloads = {
        f"components/{COMPONENT}.json": orjson.dumps(component, option=orjson.OPT_APPEND_NEWLINE),
        f"automations/actions/{ACTION}.json": orjson.dumps(
            action, option=orjson.OPT_APPEND_NEWLINE
        ),
        "components.index.json": dumps_envelope_entries_per_line(component_index),
        "automations.index.json": dumps_envelope_entries_per_line(automation_index),
    }
    changed = []
    for relative, payload in payloads.items():
        target = definitions / relative
        if target.exists() and json.loads(target.read_bytes()) == json.loads(payload):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_suffix(".json.next")
        staging.write_bytes(payload)
        staging.replace(target)
        changed.append(relative)
    return changed


def _replace_entry(entries: list[dict], replacement: dict) -> None:
    for i, entry in enumerate(entries):
        if entry["id"] == replacement["id"]:
            entries[i] = copy.deepcopy(replacement)
            return
    entries.append(copy.deepcopy(replacement))


def main() -> None:
    """Read an exact-core schema export and refresh a build-time catalog copy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-dir", type=Path, required=True)
    parser.add_argument("--definitions-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    print(json.dumps(refresh(args.definitions_dir, args.schema_dir, args.version), indent=2))


if __name__ == "__main__":
    main()
