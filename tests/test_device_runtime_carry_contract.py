"""Pin the rebuild-preservation classification of every ``Device`` field."""

from __future__ import annotations

import dataclasses

from esphome_device_builder.models import RUNTIME_CARRY_FIELDS, Device

# How each field survives a ``load_device_from_storage`` rebuild:
#   derived   — recomputed from the YAML / StorageJSON / other fields
#   persisted — passed back in from the metadata store / shared sidecar
#   carried   — copied from the previous in-memory Device (live
#               monitor observation; RUNTIME_CARRY_FIELDS)
# A new Device field must be added here with a conscious choice, or the
# coverage test fails — forgetting the choice is how a monitor-observed
# field silently resets on every rebuild (issue #1939, ``active_source``).
_FIELD_PRESERVATION: dict[str, str] = {
    "name": "derived",
    "friendly_name": "derived",
    "configuration": "derived",
    "comment": "derived",
    "area": "derived",
    "board_id": "persisted",
    "target_platform": "derived",
    "address": "derived",
    "ip": "persisted",
    "ip_addresses": "carried",
    "web_port": "derived",
    "current_version": "derived",
    "deployed_version": "carried",
    "queued_update": "carried",
    "expected_config_hash": "persisted",
    "deployed_config_hash": "carried",
    "loaded_integrations": "derived",
    "directly_referenced_integrations": "derived",
    "state": "carried",
    "active_source": "carried",
    "has_pending_changes": "derived",
    "pending_changes_via_hash": "derived",
    "update_available": "derived",
    "uses_mqtt": "derived",
    "api_enabled": "derived",
    "api_encrypted": "derived",
    "api_encryption_active": "carried",
    "mac_address": "persisted",
    "ethernet_mac": "derived",
    "bluetooth_mac": "derived",
    "build_size_bytes": "persisted",
    "labels": "persisted",
    "logger_baud_rate": "derived",
    "ota_partition_access": "derived",
}


def test_every_device_field_is_classified() -> None:
    model_fields = {f.name for f in dataclasses.fields(Device)}
    assert model_fields == set(_FIELD_PRESERVATION)


def test_carried_classification_matches_runtime_carry_fields() -> None:
    carried = {name for name, how in _FIELD_PRESERVATION.items() if how == "carried"}
    assert carried == set(RUNTIME_CARRY_FIELDS)


def test_classifications_use_known_labels() -> None:
    assert set(_FIELD_PRESERVATION.values()) <= {"derived", "persisted", "carried"}
