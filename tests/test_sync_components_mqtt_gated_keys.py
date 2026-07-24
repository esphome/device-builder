"""``cv.requires_component`` gates are auto-discovered from the live schema (#2300)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _apply_component_gates,
    _collect_component_gates,
)


@pytest.fixture
def cv():
    """Lazy-import esphome's config_validation; skip if unavailable."""
    try:
        from esphome import config_validation as _cv  # noqa: PLC0415
    except Exception:
        pytest.skip("esphome.config_validation not importable")
    return _cv


def _manifest(schema) -> SimpleNamespace:
    return SimpleNamespace(config_schema=schema)


def test_command_retain_gate_is_discovered(cv) -> None:
    """The #2300 field: ``All(requires_component("mqtt"), boolean)`` yields the gate."""
    schema = cv.Schema(
        {cv.Optional("command_retain"): cv.All(cv.requires_component("mqtt"), cv.boolean)}
    )
    assert _collect_component_gates(_manifest(schema)) == {("command_retain",): "mqtt"}


def test_core_mqtt_command_schema_gates_every_field(cv) -> None:
    """The real upstream schema yields the full MQTT entity-option inventory."""
    gates = _collect_component_gates(_manifest(cv.MQTT_COMMAND_COMPONENT_SCHEMA))
    expected = {
        "qos",
        "retain",
        "discovery",
        "subscribe_qos",
        "state_topic",
        "command_topic",
        "command_retain",
        "availability",
    }
    assert {path[0] for path, gate in gates.items() if gate == "mqtt"} >= expected


def test_first_gate_in_a_multi_requires_chain_wins(cv) -> None:
    schema = cv.Schema(
        {
            cv.Optional("report"): cv.All(
                cv.requires_component("zigbee"), cv.requires_component("esp32"), cv.boolean
            )
        }
    )
    assert _collect_component_gates(_manifest(schema)) == {("report",): "zigbee"}


def test_ungated_fields_yield_nothing(cv) -> None:
    """Plain validators (sensor's ``force_update``) must not grow a gate."""
    schema = cv.Schema({cv.Optional("force_update", default=False): cv.boolean})
    assert _collect_component_gates(_manifest(schema)) == {}


def test_apply_stamps_matching_paths() -> None:
    entries = [
        {"key": "command_retain", "depends_on_component": None},
        {"key": "name", "depends_on_component": None},
    ]
    _apply_component_gates(entries, {("command_retain",): "mqtt"})
    assert entries[0]["depends_on_component"] == "mqtt"
    assert entries[1]["depends_on_component"] is None


def test_apply_never_overrides_an_explicit_gate() -> None:
    entries = [{"key": "command_retain", "depends_on_component": "custom"}]
    _apply_component_gates(entries, {("command_retain",): "mqtt"})
    assert entries[0]["depends_on_component"] == "custom"
