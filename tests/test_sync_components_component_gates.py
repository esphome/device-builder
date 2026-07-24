"""Cross-component gates are auto-discovered from the live schema, not hand-listed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from script.sync_components import (  # type: ignore[import-not-found]
    _apply_component_gates,
    _collect_component_gates,
    introspect_component,
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


def test_requires_component_gate_is_discovered(cv) -> None:
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


def test_only_with_key_marker_is_discovered(cv) -> None:
    schema = cv.Schema({cv.OnlyWith("web_server_id", "web_server"): cv.string})
    assert _collect_component_gates(_manifest(schema)) == {("web_server_id",): "web_server"}


def test_only_with_component_list_skips_chip_platforms(cv) -> None:
    schema = cv.Schema({cv.OnlyWith("zigbee_switch", ["nrf52", "zigbee"]): cv.string})
    assert _collect_component_gates(_manifest(schema)) == {("zigbee_switch",): "zigbee"}


def test_only_without_is_not_a_gate(cv) -> None:
    """``OnlyWithout`` keys on component absence; gating them would invert the meaning."""
    schema = cv.Schema({cv.OnlyWithout("fallback", "wifi", default=True): cv.boolean})
    assert _collect_component_gates(_manifest(schema)) == {}


def test_container_with_unanimously_gated_children_inherits(cv) -> None:
    schema = cv.Schema(
        {
            cv.Optional("web_server"): cv.Schema(
                {
                    cv.OnlyWith("web_server_id", "web_server"): cv.string,
                    cv.Optional("sorting_weight"): cv.All(
                        cv.requires_component("web_server"), cv.float_
                    ),
                }
            )
        }
    )
    gates = _collect_component_gates(_manifest(schema))
    assert gates[("web_server",)] == "web_server"
    assert gates[("web_server", "web_server_id")] == "web_server"
    assert gates[("web_server", "sorting_weight")] == "web_server"


def test_container_with_mixed_children_is_not_gated(cv) -> None:
    schema = cv.Schema(
        {
            cv.Optional("box"): cv.Schema(
                {
                    cv.Optional("gated"): cv.All(cv.requires_component("mqtt"), cv.boolean),
                    cv.Optional("plain"): cv.boolean,
                }
            )
        }
    )
    gates = _collect_component_gates(_manifest(schema))
    assert ("box",) not in gates
    assert gates[("box", "gated")] == "mqtt"


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


def test_live_switch_platform_derives_the_mqtt_gates(cv) -> None:
    """The installed esphome's real gpio manifests yield the entity gates."""
    gates = introspect_component("gpio").get("component_gates") or {}
    assert gates.get(("command_retain",)) == "mqtt"
    assert gates.get(("state_topic",)) == "mqtt"
    assert gates.get(("web_server",)) == "web_server"
    assert gates.get(("web_server", "web_server_id")) == "web_server"
    assert gates.get(("zigbee_switch",)) == "zigbee"


def test_live_mqtt_component_has_no_self_gate(cv) -> None:
    """The mqtt component's own fields carry no gate on mqtt itself."""
    gates = introspect_component("mqtt").get("component_gates") or {}
    assert ("discovery",) not in gates
