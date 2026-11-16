"""Adafruit BLE catalog generation contracts."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from esphome.const import __version__

from esphome_device_builder.models.automations import AutomationAction
from esphome_device_builder.models.components import ComponentCatalogEntry
from script import sync_components as sync
from script.refresh_adafruit_ble import ACTION, COMPONENT, refresh


def test_manual_action_description(tmp_path):
    action = sync._convert_automation_action(
        top_key="adafruit_ble.ota",
        domain=COMPONENT,
        wire_prefix=COMPONENT,
        name="enter_bootloader",
        body={
            "maybe": "id",
            "schema": {
                "config_vars": {
                    "id": {
                        "key": "GeneratedID",
                        "type": "use_id",
                        "use_id_type": "adafruit_ble::OTAComponent",
                    }
                }
            },
        },
        schema_dir=tmp_path,
    )
    assert action is not None
    assert action["description"] == (
        "Restart into the Adafruit BLE bootloader; "
        "finish any shutdown actions before calling this action."
    )
    assert action["scalar_shorthand_key"] == "id"
    assert action["domain"] == COMPONENT
    assert not action["config_entries"][0].get("required")


def test_ble_trigger_description_and_visibility(tmp_path):
    fields = sync._convert_config_vars(
        {
            "config_vars": {
                "ble_trigger": {
                    "key": "Optional",
                    "type": "boolean",
                    "default": "True",
                }
            }
        },
        tmp_path,
        component_id=COMPONENT,
    )
    field = fields[0]
    assert field["description"] == (
        "Enable the BLE service for requesting a bootloader update; "
        "optional and enabled by default."
    )
    assert field["default_value"] is True
    assert not field["required"]
    assert not field["advanced"]
    assert not field.get("depends_on_component")


@pytest.mark.parametrize("component", [COMPONENT, "ota.other"])
def test_missing_field_is_not_invented(tmp_path, component):
    assert (
        sync._convert_config_vars(
            {"config_vars": {}},
            tmp_path,
            component_id=component,
        )
        == []
    )


def test_other_component_field_is_unchanged(tmp_path):
    field = sync._convert_config_vars(
        {"config_vars": {"ble_trigger": {"key": "Optional", "type": "boolean"}}},
        tmp_path,
        component_id="ota.other",
    )[0]
    assert not field.get("description")


@pytest.mark.parametrize("name,docs", [("other", ""), ("enter_bootloader", "Existing help.")])
def test_action_help_is_scoped_and_preserves_schema_docs(tmp_path, name, docs):
    action = sync._convert_automation_action(
        top_key="adafruit_ble.ota",
        domain=COMPONENT,
        wire_prefix=COMPONENT,
        name=name,
        body={"docs": docs, "schema": {"config_vars": {}}},
        schema_dir=tmp_path,
    )
    assert action is not None
    assert action["description"] == docs


@pytest.fixture
def schema_dir():
    value = os.environ.get("ESPHOME_SCHEMA_DIR")
    if not value:
        pytest.skip("Set ESPHOME_SCHEMA_DIR to an export from the installed core")
    assert value is not None
    return Path(value)


@pytest.fixture
def definitions(tmp_path):
    source = Path(__file__).resolve().parents[1] / "esphome_device_builder" / "definitions"
    target = tmp_path / "definitions"
    shutil.copytree(source, target)
    return target


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _feature(schema_dir):
    path = schema_dir / "adafruit_ble.json"
    return path.exists() and "enter_bootloader" in path.read_text()


def test_real_export_scoped_refresh_and_idempotence(schema_dir, definitions):
    before = _snapshot(definitions)
    changed = refresh(definitions, schema_dir, __version__)
    if not _feature(schema_dir):
        assert changed == []
        assert _snapshot(definitions) == before
        return
    allowed = {
        f"components/{COMPONENT}.json",
        f"automations/actions/{ACTION}.json",
        "components.index.json",
        "automations.index.json",
    }
    assert set(changed) <= allowed
    after = _snapshot(definitions)
    assert {p for p in before if before[p] != after.get(p)} <= allowed
    assert set(after) - set(before) <= allowed
    for filename, kind, changed_id in [
        ("components.index.json", "components", COMPONENT),
        ("automations.index.json", "actions", ACTION),
    ]:
        old, new = json.loads(before[filename]), json.loads(after[filename])
        assert [{k: v for k, v in old.items() if k != kind}] == [
            {k: v for k, v in new.items() if k != kind}
        ]
        assert [e for e in old[kind] if e["id"] != changed_id] == [
            e for e in new[kind] if e["id"] != changed_id
        ]
    assert refresh(definitions, schema_dir, __version__) == []
    assert _snapshot(definitions) == after


def test_real_export_models_and_dependency_metadata(schema_dir, definitions):
    if not _feature(schema_dir):
        pytest.skip("Core has no manual bootloader entry")
    refresh(definitions, schema_dir, __version__)
    component = ComponentCatalogEntry.from_dict(
        json.loads((definitions / f"components/{COMPONENT}.json").read_bytes())
    )
    action = AutomationAction.from_dict(
        json.loads((definitions / f"automations/actions/{ACTION}.json").read_bytes())
    )
    assert component.dependencies == ["nrf52"]
    assert component.supported_platforms == ["nrf52"]
    assert "adafruit_ble" in component.provides
    assert action.domain == COMPONENT
    assert action.scalar_shorthand_key == "id"
    assert action.config_entries[0].references_component in component.provides
    fields = {e.key: e for e in component.config_entries}
    assert fields["ble_trigger"].default_value is True
    assert not fields["ble_trigger"].depends_on_component
    assert not fields["ble_trigger"].required
    assert "pin" not in fields
    assert "zephyr_ble_server" not in component.dependencies
    assert "zephyr_ble_server" in component.description


def test_real_export_description_only_refresh(schema_dir, definitions):
    if not _feature(schema_dir):
        pytest.skip("Core has no manual bootloader entry")
    refresh(definitions, schema_dir, __version__)
    path = definitions / f"components/{COMPONENT}.json"
    body = json.loads(path.read_bytes())
    next(e for e in body["config_entries"] if e["key"] == "ble_trigger").pop("description")
    path.write_text(json.dumps(body))
    assert refresh(definitions, schema_dir, __version__) == [f"components/{COMPONENT}.json"]
    assert refresh(definitions, schema_dir, __version__) == []


def test_version_mismatch_never_writes(schema_dir, definitions):
    before = _snapshot(definitions)
    with pytest.raises(SystemExit):
        refresh(definitions, schema_dir, "0.0.0-invalid")
    assert _snapshot(definitions) == before


def test_capability_mismatch_never_writes(schema_dir, definitions, tmp_path):
    if not _feature(schema_dir):
        pytest.skip("Core has no manual bootloader entry")
    empty = tmp_path / "empty-schema"
    empty.mkdir()
    before = _snapshot(definitions)
    with pytest.raises(ValueError, match="capabilities differ"):
        refresh(definitions, empty, __version__)
    assert _snapshot(definitions) == before
