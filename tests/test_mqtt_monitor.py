"""
Tests for MQTT detection, broker config parsing, and the multi-broker coordinator.

Covers the parts that don't require a live broker:
* YAML parsing for the ``mqtt:`` opt-in (helpers.device_yaml)
* ``parse_mqtt_block`` — broker extraction with ``!secret`` resolution
* ``DeviceMqttCoordinator`` — start/stop one monitor per unique broker
* Source-priority logic in ``DeviceStateMonitor`` (mdns > mqtt > ping)
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from esphome_device_builder.controllers._device_mqtt_coordinator import (
    DeviceMqttCoordinator,
    parse_mqtt_block,
)
from esphome_device_builder.controllers._device_mqtt_monitor import (
    DeviceMqttMonitor,
    MqttBrokerConfig,
)
from esphome_device_builder.controllers._device_state_monitor import DeviceStateMonitor
from esphome_device_builder.helpers.device_yaml import device_uses_mqtt
from esphome_device_builder.models import Device, DeviceState

# ---------------------------------------------------------------------------
# YAML detection
# ---------------------------------------------------------------------------


def test_device_uses_mqtt_top_level_block() -> None:
    yaml = "esphome:\n  name: foo\n\nmqtt:\n  broker: 192.168.1.10\n"
    assert device_uses_mqtt(yaml) is True


def test_device_uses_mqtt_with_comment_above() -> None:
    yaml = "# notes\n\nmqtt:\n  broker: x\n"
    assert device_uses_mqtt(yaml) is True


def test_device_uses_mqtt_inline_token_does_not_count() -> None:
    yaml = "esphome:\n  name: foo\n  comment: 'uses mqtt for telemetry'\n"
    assert device_uses_mqtt(yaml) is False


def test_device_uses_mqtt_only_indented_block() -> None:
    # Indented ``mqtt:`` is part of another block (e.g. a sensor config),
    # not an opt-in to dashboard MQTT discovery.
    yaml = "esphome:\n  name: foo\n\nsensor:\n  - mqtt:\n      topic: foo\n"
    assert device_uses_mqtt(yaml) is False


def test_device_uses_mqtt_handles_empty_input() -> None:
    assert device_uses_mqtt("") is False


# ---------------------------------------------------------------------------
# parse_mqtt_block — broker extraction
# ---------------------------------------------------------------------------


def test_parse_mqtt_block_simple() -> None:
    yaml = "mqtt:\n  broker: 192.168.1.10\n  username: user\n  password: pass\n"
    config = parse_mqtt_block(yaml)
    assert config == MqttBrokerConfig(
        host="192.168.1.10",
        port=1883,
        username="user",
        password="pass",  # noqa: S106 — fixture credential
    )


def test_parse_mqtt_block_custom_port() -> None:
    yaml = "mqtt:\n  broker: broker.example\n  port: 8883\n"
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.port == 8883


def test_parse_mqtt_block_resolves_secrets() -> None:
    yaml = "mqtt:\n  broker: !secret broker_host\n  password: !secret pw\n"
    secrets = {"broker_host": "192.168.1.5", "pw": "topsecret"}
    config = parse_mqtt_block(yaml, secrets)
    assert config is not None
    assert config.host == "192.168.1.5"
    assert config.password == "topsecret"  # noqa: S105 — fixture credential


def test_parse_mqtt_block_missing_secret_returns_none() -> None:
    # broker is required; if its secret can't be resolved, the whole
    # block is unusable.
    yaml = "mqtt:\n  broker: !secret missing\n"
    assert parse_mqtt_block(yaml, {}) is None


def test_parse_mqtt_block_no_block() -> None:
    yaml = "esphome:\n  name: foo\n"
    assert parse_mqtt_block(yaml) is None


def test_parse_mqtt_block_ignores_unknown_tags() -> None:
    # Devices can use ESPHome custom tags (!lambda, !include) that pyyaml
    # doesn't know about — parsing must not raise.
    yaml = (
        "esphome:\n  name: foo\n"
        "sensor:\n  - platform: template\n    lambda: !lambda 'return 1;'\n"
        "mqtt:\n  broker: broker.local\n"
    )
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.host == "broker.local"


def test_parse_mqtt_block_invalid_yaml_returns_none() -> None:
    assert parse_mqtt_block("not: valid: yaml: at all") is None


def test_mqtt_broker_config_key_groups_by_host_port() -> None:
    a = MqttBrokerConfig(host="broker", port=1883, username="alice")
    b = MqttBrokerConfig(host="broker", port=1883, username="bob")
    c = MqttBrokerConfig(host="broker", port=8883, username="alice")
    assert a.key == b.key
    assert a.key != c.key


# ---------------------------------------------------------------------------
# DeviceMqttCoordinator — broker session lifecycle
# ---------------------------------------------------------------------------


class _RecordingMonitor:
    """Stand-in for ``DeviceMqttMonitor`` that records lifecycle calls."""

    instances: ClassVar[list[_RecordingMonitor]] = []

    def __init__(self, broker: MqttBrokerConfig, *_args: object, **_kwargs: object) -> None:
        self.broker = broker
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    @staticmethod
    def is_available() -> bool:
        return True

    @property
    def running(self) -> bool:
        return self.started and not self.stopped

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def stub_monitor(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingMonitor]:
    _RecordingMonitor.instances = []
    monkeypatch.setattr(
        "esphome_device_builder.controllers._device_mqtt_coordinator.DeviceMqttMonitor",
        _RecordingMonitor,
    )
    return _RecordingMonitor


def _write_device(config_dir: Path, name: str, mqtt_yaml: str | None) -> Device:
    yaml = f"esphome:\n  name: {name}\n"
    if mqtt_yaml is not None:
        yaml += f"\n{mqtt_yaml}"
    (config_dir / f"{name}.yaml").write_text(yaml)
    return Device(
        name=name,
        friendly_name=name,
        configuration=f"{name}.yaml",
        uses_mqtt=mqtt_yaml is not None,
    )


def _make_coordinator(config_dir: Path, devices: list[Device]) -> DeviceMqttCoordinator:
    return DeviceMqttCoordinator(
        config_dir=config_dir,
        get_devices=lambda: devices,
        on_state_change=lambda *_args: None,
        on_ip_change=lambda *_args: None,
    )


async def test_coordinator_no_mqtt_devices_runs_no_monitors(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    devices = [_write_device(tmp_path, "plain", None)]
    coord = _make_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 0
    assert stub_monitor.instances == []


async def test_coordinator_groups_devices_with_same_broker(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    devices = [
        _write_device(tmp_path, "alpha", "mqtt:\n  broker: 192.168.1.10\n"),
        _write_device(tmp_path, "beta", "mqtt:\n  broker: 192.168.1.10\n"),
    ]
    coord = _make_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert len(stub_monitor.instances) == 1
    assert stub_monitor.instances[0].broker.host == "192.168.1.10"


async def test_coordinator_starts_one_monitor_per_unique_broker(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    devices = [
        _write_device(tmp_path, "alpha", "mqtt:\n  broker: broker-a.local\n"),
        _write_device(tmp_path, "beta", "mqtt:\n  broker: broker-b.local\n  port: 8883\n"),
    ]
    coord = _make_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 2
    hosts = sorted(m.broker.host for m in stub_monitor.instances)
    assert hosts == ["broker-a.local", "broker-b.local"]


async def test_coordinator_stops_monitors_when_devices_drop_mqtt(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    devices = [_write_device(tmp_path, "alpha", "mqtt:\n  broker: broker.local\n")]
    coord = _make_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 1

    # Simulate the user editing the YAML to remove the mqtt: block.
    devices[0].uses_mqtt = False
    await coord.reconcile()
    assert coord.active_brokers == 0
    assert stub_monitor.instances[0].stopped is True


async def test_coordinator_stop_cleans_up_all_monitors(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    devices = [
        _write_device(tmp_path, "alpha", "mqtt:\n  broker: broker-a.local\n"),
        _write_device(tmp_path, "beta", "mqtt:\n  broker: broker-b.local\n"),
    ]
    coord = _make_coordinator(tmp_path, devices)
    await coord.reconcile()
    await coord.stop()
    assert coord.active_brokers == 0
    assert all(m.stopped for m in stub_monitor.instances)


async def test_coordinator_skips_devices_with_unresolvable_secrets(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    devices = [_write_device(tmp_path, "alpha", "mqtt:\n  broker: !secret missing\n")]
    coord = _make_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 0


async def test_coordinator_resolves_secrets_from_secrets_yaml(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    (tmp_path / "secrets.yaml").write_text("mqtt_broker: 10.0.0.5\nmqtt_pw: shh\n")
    devices = [
        _write_device(
            tmp_path,
            "alpha",
            "mqtt:\n  broker: !secret mqtt_broker\n  password: !secret mqtt_pw\n",
        )
    ]
    coord = _make_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "10.0.0.5"
    assert stub_monitor.instances[0].broker.password == "shh"  # noqa: S105 — fixture credential


# ---------------------------------------------------------------------------
# DeviceMqttMonitor — solo lifecycle
# ---------------------------------------------------------------------------


def test_monitor_running_flag_is_false_before_start() -> None:
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_args: None,
        on_ip_change=lambda *_args: None,
    )
    assert monitor.running is False


async def test_monitor_stop_without_start_is_noop() -> None:
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_args: None,
        on_ip_change=lambda *_args: None,
    )
    await monitor.stop()
    assert monitor.running is False


# ---------------------------------------------------------------------------
# DeviceStateMonitor — source priority
# ---------------------------------------------------------------------------


def _build_state_monitor() -> tuple[
    DeviceStateMonitor, list[Device], list[tuple[str, DeviceState, str]]
]:
    devices = [Device(name="alpha", friendly_name="Alpha", configuration="alpha.yaml")]
    transitions: list[tuple[str, DeviceState, str]] = []

    def record(name: str, state: DeviceState, source: str) -> None:
        transitions.append((name, state, source))
        for device in devices:
            if device.name == name:
                device.state = state

    monitor = DeviceStateMonitor(
        get_devices=lambda: devices,
        on_state_change=record,
        on_ip_change=lambda _n, _ip: None,
    )
    return monitor, devices, transitions


def test_priority_mdns_blocks_lower_sources() -> None:
    monitor, _, transitions = _build_state_monitor()

    assert monitor.apply("alpha", DeviceState.ONLINE, "mdns") is True
    assert monitor.apply("alpha", DeviceState.OFFLINE, "mqtt") is False
    assert monitor.apply("alpha", DeviceState.OFFLINE, "ping") is False
    assert transitions == [("alpha", DeviceState.ONLINE, "mdns")]


def test_priority_mqtt_overrides_ping() -> None:
    monitor, _, transitions = _build_state_monitor()

    assert monitor.apply("alpha", DeviceState.ONLINE, "ping") is True
    assert monitor.apply("alpha", DeviceState.OFFLINE, "mqtt") is True
    assert transitions[-1] == ("alpha", DeviceState.OFFLINE, "mqtt")


def test_priority_same_source_replays_are_noop_for_identical_state() -> None:
    monitor, _, transitions = _build_state_monitor()

    assert monitor.apply("alpha", DeviceState.ONLINE, "mqtt") is True
    assert monitor.apply("alpha", DeviceState.ONLINE, "mqtt") is False
    assert len(transitions) == 1


def test_priority_unknown_source_stamped_after_first_observation() -> None:
    monitor, _, _ = _build_state_monitor()

    assert monitor.apply("alpha", DeviceState.ONLINE, "ping") is True
    assert monitor.priority_for("alpha") == "ping"


def test_unknown_device_observation_is_ignored() -> None:
    monitor, _, transitions = _build_state_monitor()
    assert monitor.apply("missing", DeviceState.ONLINE, "mqtt") is False
    assert transitions == []


def test_ping_can_rescue_after_mdns_offline() -> None:
    """After mDNS pops its source, ping must be allowed to re-mark ONLINE."""
    monitor, _, transitions = _build_state_monitor()
    monitor.apply("alpha", DeviceState.ONLINE, "mdns")
    monitor.apply("alpha", DeviceState.OFFLINE, "mdns")
    # The mDNS Removed handler clears the source so a different source can take over.
    monitor._state_source.pop("alpha", None)
    assert monitor.apply("alpha", DeviceState.ONLINE, "ping") is True
    assert transitions[-1] == ("alpha", DeviceState.ONLINE, "ping")
