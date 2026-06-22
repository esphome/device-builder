"""
Tests for MQTT detection, broker config parsing, and the multi-broker coordinator.

Covers the parts that don't require a live broker:
* YAML parsing for the ``mqtt:`` opt-in (helpers.device_yaml)
* ``parse_mqtt_block`` — broker extraction with ``!secret`` resolution
* ``DeviceMqttCoordinator`` — start/stop one monitor per unique broker
* Source-priority logic in ``DeviceStateMonitor`` (mdns > mqtt > ping)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import pytest

from esphome_device_builder.controllers import (
    _device_mqtt_coordinator as coordinator_module,
)
from esphome_device_builder.controllers import _device_mqtt_monitor as monitor_module
from esphome_device_builder.controllers._device_mqtt_coordinator import (
    DeviceMqttCoordinator,
    _extract_broker_from_config,
    parse_mqtt_block,
)
from esphome_device_builder.controllers._device_mqtt_monitor import (
    DeviceMqttMonitor,
    MqttBrokerConfig,
    _decode_payload,
    _extract_ip,
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
        password="pass",
    )


def test_parse_mqtt_block_custom_port() -> None:
    yaml = "mqtt:\n  broker: broker.example\n  port: 8883\n"
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.port == 8883


def test_parse_mqtt_block_resolves_secrets() -> None:
    yaml = "mqtt:\n  broker: !secret broker_host\n  port: !secret port\n  password: !secret pw\n"
    secrets = {"broker_host": "192.168.1.5", "port": "8883", "pw": "topsecret"}
    config = parse_mqtt_block(yaml, secrets)
    assert config is not None
    assert config.host == "192.168.1.5"
    assert config.port == 8883
    assert config.password == "topsecret"


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


def test_parse_mqtt_block_resolves_substitutions() -> None:
    yaml = (
        "substitutions:\n  mqtt_host: 192.0.2.10\n  mqtt_user: bob\n  mqtt_port: '8883'\n"
        "mqtt:\n  broker: ${mqtt_host}\n  port: ${mqtt_port}\n  username: $mqtt_user\n"
    )
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.host == "192.0.2.10"
    assert config.port == 8883
    assert config.username == "bob"


def test_parse_mqtt_block_unresolved_substitution_returns_none() -> None:
    # No local substitutions block defines mqtt_host; the token must not
    # become a host, so the caller falls through to the slow path.
    yaml = "mqtt:\n  broker: ${mqtt_host}\n"
    assert parse_mqtt_block(yaml) is None


def test_parse_mqtt_block_resolves_port_substitution() -> None:
    yaml = "substitutions:\n  mqtt_port: '8883'\nmqtt:\n  broker: 10.0.0.5\n  port: ${mqtt_port}\n"
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.port == 8883


def test_parse_mqtt_block_unresolved_port_substitution_falls_back_to_default() -> None:
    # An unresolved port token can't gate the monitor (only the host does),
    # so it degrades to the default rather than raising.
    yaml = "mqtt:\n  broker: 10.0.0.5\n  port: ${mqtt_port}\n"
    config = parse_mqtt_block(yaml)
    assert config is not None
    assert config.port == 1883


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
    assert stub_monitor.instances[0].broker.password == "shh"


async def test_coordinator_resolves_secrets_via_included_secrets_file(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # secrets.yaml pulls in a shared file via the merge-key +
    # ``!include`` pattern (HA-shared secrets); the plain SafeLoader
    # rejects it, so the ESPHome-loader fallback must resolve it.
    (tmp_path / "shared.yaml").write_text("mqtt_broker: 10.0.0.9\nmqtt_pw: shh\n")
    (tmp_path / "secrets.yaml").write_text("<<: !include shared.yaml\n")
    devices = [
        _write_device(
            tmp_path,
            "alpha",
            "mqtt:\n  broker: !secret mqtt_broker\n  password: !secret mqtt_pw\n",
        )
    ]
    coord = _make_coordinator(tmp_path, devices)
    with caplog.at_level("WARNING"):
        await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "10.0.0.9"
    assert stub_monitor.instances[0].broker.password == "shh"
    assert "Could not parse secrets.yaml" not in caplog.text


async def test_coordinator_warns_when_secrets_unparseable_by_both_loaders(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / "secrets.yaml").write_text("<<: !include does_not_exist.yaml\n")
    devices = [
        _write_device(
            tmp_path,
            "alpha",
            "mqtt:\n  broker: !secret mqtt_broker\n",
        )
    ]
    coord = _make_coordinator(tmp_path, devices)
    with caplog.at_level("WARNING"):
        await coord.reconcile()
    assert coord.active_brokers == 0
    assert "Could not read secrets.yaml" in caplog.text


async def test_coordinator_empty_secrets_yaml_does_not_warn(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An empty / comment-only secrets.yaml parses to None; that is a
    # legitimate file and must not spam a warning on every poll.
    (tmp_path / "secrets.yaml").write_text("# only a comment\n")
    devices = [_write_device(tmp_path, "alpha", "mqtt:\n  broker: !secret mqtt_broker\n")]
    coord = _make_coordinator(tmp_path, devices)
    with caplog.at_level("WARNING"):
        await coord.reconcile()
    assert coord.active_brokers == 0
    assert "secrets.yaml" not in caplog.text


async def test_coordinator_warns_when_secrets_yaml_not_a_mapping(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A secrets.yaml that parses to a list/scalar is structurally wrong;
    # warn distinctly from a parse failure rather than degrade silently.
    (tmp_path / "secrets.yaml").write_text("- not\n- a\n- mapping\n")
    devices = [_write_device(tmp_path, "alpha", "mqtt:\n  broker: !secret mqtt_broker\n")]
    coord = _make_coordinator(tmp_path, devices)
    with caplog.at_level("WARNING"):
        await coord.reconcile()
    assert coord.active_brokers == 0
    assert "secrets.yaml is not a mapping" in caplog.text


async def test_coordinator_resolves_broker_pulled_in_via_packages(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    # Issue #893: mqtt block lives in a shared package, not the
    # device file. Resolved-config fallback expands and resolves it.
    (tmp_path / "common.yaml").write_text("mqtt:\n  broker: 192.168.1.203\n")
    (tmp_path / "alpha.yaml").write_text(
        "esphome:\n  name: alpha\npackages:\n  shared: !include common.yaml\n"
    )
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
    )
    coord = _make_coordinator(tmp_path, [device])
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "192.168.1.203"


async def test_coordinator_resolves_broker_from_local_substitution(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Issue #1643: ${var} broker resolves from the file's own
    # substitutions block in the fast path, without load_device_yaml.
    def fail_loader(_path: Path) -> dict | None:
        raise AssertionError("slow path must not run for a local substitution")

    monkeypatch.setattr(coordinator_module, "load_device_yaml", fail_loader)
    devices = [
        _write_device(
            tmp_path,
            "alpha",
            "substitutions:\n  mqtt_host: 192.0.2.10\nmqtt:\n  broker: ${mqtt_host}\n",
        )
    ]
    coord = _make_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "192.0.2.10"


async def test_coordinator_resolves_port_from_substitution(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    mqtt_yaml = (
        "substitutions:\n  mqtt_port: '8883'\nmqtt:\n  broker: 10.0.0.5\n  port: ${mqtt_port}\n"
    )
    devices = [_write_device(tmp_path, "alpha", mqtt_yaml)]
    coord = _make_coordinator(tmp_path, devices)
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.port == 8883


async def test_coordinator_resolves_broker_substitution_via_package(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    (tmp_path / "common.yaml").write_text(
        "substitutions:\n  mqtt_host: 192.168.1.77\nmqtt:\n  broker: ${mqtt_host}\n"
    )
    (tmp_path / "alpha.yaml").write_text(
        "esphome:\n  name: alpha\npackages:\n  shared: !include common.yaml\n"
    )
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
    )
    coord = _make_coordinator(tmp_path, [device])
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "192.168.1.77"


async def test_coordinator_skips_unresolved_substitution(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An undefined substitution must not start a monitor on the literal
    # token; warn once instead of looping on DNS failure.
    device = _write_device(tmp_path, "alpha", "mqtt:\n  broker: ${mqtt_host}\n")
    coord = _make_coordinator(tmp_path, [device])

    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("WARNING", logger=target):
        await coord.reconcile()

    assert coord.active_brokers == 0
    warnings = [r for r in caplog.records if r.name == target and r.levelname == "WARNING"]
    assert len(warnings) == 1


async def test_coordinator_warns_once_per_unresolved_device(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # uses_mqtt set but no mqtt: present anywhere — neither path
    # can resolve a broker.
    (tmp_path / "alpha.yaml").write_text("esphome:\n  name: alpha\n")
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
    )
    coord = _make_coordinator(tmp_path, [device])

    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("DEBUG", logger=target):
        await coord.reconcile()
        await coord.reconcile()
        await coord.reconcile()

    warnings = [r for r in caplog.records if r.name == target and r.levelname == "WARNING"]
    debugs = [
        r
        for r in caplog.records
        if r.name == target
        and r.levelname == "DEBUG"
        and "still could not be resolved" in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert len(debugs) >= 2


async def test_coordinator_re_warns_after_broker_recovers_and_breaks_again(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Dedupe must reset on a successful resolve so a later
    # regression surfaces a fresh WARNING, not a DEBUG.
    alpha_path = tmp_path / "alpha.yaml"
    alpha_path.write_text("esphome:\n  name: alpha\n")
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
    )
    coord = _make_coordinator(tmp_path, [device])

    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("WARNING", logger=target):
        await coord.reconcile()  # unresolved → WARNING #1
        await coord.reconcile()  # unresolved → DEBUG (suppressed)
        alpha_path.write_text("esphome:\n  name: alpha\nmqtt:\n  broker: broker.local\n")
        await coord.reconcile()  # resolved → flag cleared
        alpha_path.write_text("esphome:\n  name: alpha\n")
        await coord.reconcile()  # unresolved again → WARNING #2

    warnings = [
        r
        for r in caplog.records
        if r.name == target
        and r.levelname == "WARNING"
        and "could not be resolved" in r.getMessage()
    ]
    assert len(warnings) == 2


def test_extract_broker_from_config_returns_none_for_non_dict() -> None:
    assert _extract_broker_from_config(None) is None
    assert _extract_broker_from_config({"mqtt": "not-a-dict"}) is None
    assert _extract_broker_from_config({}) is None


async def test_coordinator_handles_stat_race_after_successful_read(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Race: read_text succeeds, file disappears before stat(). Skip
    # silently — the WARNING is reserved for fixable configs.
    yaml_path = tmp_path / "alpha.yaml"
    yaml_path.write_text("esphome:\n  name: alpha\npackages:\n  shared: !include common.yaml\n")
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
    )
    coord = _make_coordinator(tmp_path, [device])

    real_stat = Path.stat

    def _stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == yaml_path:
            raise OSError("simulated stat race")
        return real_stat(self, *args, **kwargs)

    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with patch.object(Path, "stat", _stat), caplog.at_level("DEBUG", logger=target):
        await coord.reconcile()

    assert coord.active_brokers == 0
    warnings = [r for r in caplog.records if r.name == target and r.levelname == "WARNING"]
    assert warnings == [], [r.getMessage() for r in warnings]


async def test_coordinator_caches_resolved_broker_across_polls(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``load_device_yaml`` can ``git clone`` remote packages —
    # too expensive to run every 5 s. Once resolved, polls hit
    # the cache until an mtime moves.
    (tmp_path / "common.yaml").write_text("mqtt:\n  broker: 192.168.1.50\n")
    (tmp_path / "alpha.yaml").write_text(
        "esphome:\n  name: alpha\npackages:\n  shared: !include common.yaml\n"
    )
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
    )
    coord = _make_coordinator(tmp_path, [device])

    calls = 0
    real_loader = coordinator_module.load_device_yaml

    def counting_loader(path: Path) -> dict | None:
        nonlocal calls
        calls += 1
        return real_loader(path)

    monkeypatch.setattr(coordinator_module, "load_device_yaml", counting_loader)

    await coord.reconcile()
    await coord.reconcile()
    await coord.reconcile()
    assert calls == 1
    assert coord.active_brokers == 1


async def test_coordinator_recovers_when_negative_resolve_fixed_in_secrets(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
) -> None:
    # Failure must not be cached — fix to secrets.yaml has to
    # recover on the next poll without a restart.
    (tmp_path / "alpha.yaml").write_text(
        "esphome:\n  name: alpha\nmqtt:\n  broker: !secret mqtt_host\n"
    )
    device = Device(
        name="alpha",
        friendly_name="alpha",
        configuration="alpha.yaml",
        uses_mqtt=True,
    )
    coord = _make_coordinator(tmp_path, [device])

    await coord.reconcile()
    assert coord.active_brokers == 0

    (tmp_path / "secrets.yaml").write_text("mqtt_host: 192.168.1.42\n")
    await coord.reconcile()
    assert coord.active_brokers == 1
    assert stub_monitor.instances[0].broker.host == "192.168.1.42"


async def test_coordinator_skips_devices_with_missing_yaml(
    tmp_path: Path,
    stub_monitor: type[_RecordingMonitor],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # YAML deleted between scans — skip silently, don't fire
    # the broker-unresolvable WARNING (reserved for fixable YAMLs).
    device = Device(
        name="ghost",
        friendly_name="ghost",
        configuration="ghost.yaml",
        uses_mqtt=True,
    )
    coord = _make_coordinator(tmp_path, [device])
    target = "esphome_device_builder.controllers._device_mqtt_coordinator"
    with caplog.at_level("DEBUG", logger=target):
        await coord.reconcile()
    assert coord.active_brokers == 0
    warnings = [r for r in caplog.records if r.name == target and r.levelname == "WARNING"]
    assert warnings == [], [r.getMessage() for r in warnings]


def test_extract_broker_from_config_handles_invalid_port() -> None:
    config = {"mqtt": {"broker": "broker.local", "port": "not-a-number"}}
    broker = _extract_broker_from_config(config)
    assert broker is not None
    assert broker.port == 1883


def test_extract_broker_from_config_returns_none_when_broker_missing() -> None:
    assert _extract_broker_from_config({"mqtt": {"username": "u"}}) is None


def test_extract_broker_from_config_resolves_substitutions() -> None:
    # load_device_yaml merges packages but skips the substitution pass.
    config = {
        "substitutions": {"mqtt_host": "192.168.1.203", "mqtt_port": "8883"},
        "mqtt": {"broker": "${mqtt_host}", "port": "${mqtt_port}"},
    }
    broker = _extract_broker_from_config(config)
    assert broker is not None
    assert broker.host == "192.168.1.203"
    assert broker.port == 8883


def test_extract_broker_from_config_unresolved_substitution_returns_none() -> None:
    assert _extract_broker_from_config({"mqtt": {"broker": "${mqtt_host}"}}) is None


def test_extract_broker_from_config_resolves_port_substitution() -> None:
    config = {
        "substitutions": {"mqtt_port": "8883"},
        "mqtt": {"broker": "10.0.0.5", "port": "${mqtt_port}"},
    }
    broker = _extract_broker_from_config(config)
    assert broker is not None
    assert broker.port == 8883


def test_extract_broker_from_config_reads_resolved_block() -> None:
    # Post-resolver shape — every field a plain scalar.
    config = {
        "mqtt": {
            "broker": "192.168.1.203",
            "port": 1883,
            "username": "mquser",
            "password": "topsecret",
        }
    }
    broker = _extract_broker_from_config(config)
    assert broker == MqttBrokerConfig(
        host="192.168.1.203",
        port=1883,
        username="mquser",
        password="topsecret",
    )


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
    monitor.state.state_source.pop("alpha", None)
    assert monitor.apply("alpha", DeviceState.ONLINE, "ping") is True
    assert transitions[-1] == ("alpha", DeviceState.ONLINE, "ping")


# ---------------------------------------------------------------------------
# DeviceMqttMonitor._listen — retained-message filtering
# ---------------------------------------------------------------------------


async def test_listen_drops_retained_discover_messages() -> None:
    """A retained ``esphome/discover/<name>`` must not flip the device online.

    Retained messages get delivered the moment we subscribe — they're a
    snapshot of the device's *last* publish, not proof that it's reachable
    now. Treating one as an online observation ghost-onlines a dead
    device until the offline timeout catches up.

    Synchronisation: queue a retained message followed by a fresh one
    and only assert after the fresh message's callback fires. That
    proves ``_listen`` actually drained the queue past the retained
    entry rather than racing the cancel — no ``sleep(0)`` heuristics.
    """
    state_calls: list[tuple[str, DeviceState]] = []
    fresh_seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        fresh_seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda *_: None,
    )

    class _RetainedMessage:
        topic = "esphome/discover/stress-esp32"
        payload = json.dumps({"name": "stress-esp32", "ip": "10.0.0.1"}).encode()
        retain = True

    class _FreshMessage:
        topic = "esphome/discover/kitchen"
        payload = json.dumps({"name": "kitchen", "ip": "10.0.0.2"}).encode()
        retain = False

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_RetainedMessage())
    await queue.put(_FreshMessage())

    listen_task = asyncio.create_task(monitor._listen(queue))
    try:
        await asyncio.wait_for(fresh_seen.wait(), timeout=1.0)
    finally:
        listen_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listen_task

    # Only the fresh message produced a callback — the retained one was dropped.
    assert state_calls == [("kitchen", DeviceState.ONLINE)]


async def test_listen_skips_empty_payload() -> None:
    """A message with an empty/None payload is silently skipped.

    ``_decode_payload`` returns ``""`` for ``None`` / unsupported
    shapes. The listen loop short-circuits on the falsy return so
    a misbehaving broker that sends headers without a payload
    doesn't crash the JSON parser. Pin: an empty fresh message
    followed by a real one only fires the real one's callback.
    """
    state_calls: list[tuple[str, DeviceState]] = []
    fresh_seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        fresh_seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda *_: None,
    )

    class _EmptyPayloadMessage:
        topic = "esphome/discover/ghost"
        payload = None  # _decode_payload returns ""
        retain = False

    class _FreshMessage:
        topic = "esphome/discover/kitchen"
        payload = json.dumps({"name": "kitchen", "ip": "10.0.0.7"}).encode()
        retain = False

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_EmptyPayloadMessage())
    await queue.put(_FreshMessage())

    listen_task = asyncio.create_task(monitor._listen(queue))
    try:
        await asyncio.wait_for(fresh_seen.wait(), timeout=1.0)
    finally:
        listen_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listen_task

    assert state_calls == [("kitchen", DeviceState.ONLINE)]


async def test_listen_drops_non_json_payload(caplog: pytest.LogCaptureFixture) -> None:
    """A payload that fails ``json.loads`` is logged and skipped, not raised.

    Misbehaving devices or unrelated retained messages on the
    discover topic shouldn't tank the listener. Pin: malformed
    JSON before a clean message, only the clean one fires.
    """
    state_calls: list[tuple[str, DeviceState]] = []
    fresh_seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        fresh_seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda *_: None,
    )

    class _BadJsonMessage:
        topic = "esphome/discover/garbled"
        payload = b"not-json-at-all{"
        retain = False

    class _FreshMessage:
        topic = "esphome/discover/kitchen"
        payload = json.dumps({"name": "kitchen"}).encode()
        retain = False

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_BadJsonMessage())
    await queue.put(_FreshMessage())

    with caplog.at_level("DEBUG", logger="esphome_device_builder.controllers._device_mqtt_monitor"):
        listen_task = asyncio.create_task(monitor._listen(queue))
        try:
            await asyncio.wait_for(fresh_seen.wait(), timeout=1.0)
        finally:
            listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listen_task

    assert state_calls == [("kitchen", DeviceState.ONLINE)]
    # Pin the log emission too — without this, a regression that
    # silently swallows the JSONDecodeError without recording it
    # would still pass the "fresh message wins" check.
    assert any(
        "Ignoring non-JSON payload" in rec.message and rec.levelname == "DEBUG"
        for rec in caplog.records
    ), [rec.message for rec in caplog.records]


async def test_listen_skips_payload_with_missing_or_invalid_name() -> None:
    """A payload that doesn't carry a non-empty string ``name`` is skipped.

    Defensive: a malformed firmware publishing
    ``{"ip": "..."}`` without a name has no key to associate the
    state change with. The listener silently drops it rather
    than calling the state callback with ``None`` (which the
    downstream monitor would then index by).
    """
    state_calls: list[tuple[str, DeviceState]] = []
    fresh_seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        fresh_seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda *_: None,
    )

    class _NoNameMessage:
        topic = "esphome/discover/anonymous"
        payload = json.dumps({"ip": "10.0.0.1"}).encode()  # no ``name``
        retain = False

    class _EmptyNameMessage:
        topic = "esphome/discover/blank"
        payload = json.dumps({"name": "", "ip": "10.0.0.2"}).encode()
        retain = False

    class _NumericNameMessage:
        topic = "esphome/discover/typo"
        payload = json.dumps({"name": 42}).encode()  # not a string
        retain = False

    class _FreshMessage:
        topic = "esphome/discover/kitchen"
        payload = json.dumps({"name": "kitchen"}).encode()
        retain = False

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_NoNameMessage())
    await queue.put(_EmptyNameMessage())
    await queue.put(_NumericNameMessage())
    await queue.put(_FreshMessage())

    listen_task = asyncio.create_task(monitor._listen(queue))
    try:
        await asyncio.wait_for(fresh_seen.wait(), timeout=1.0)
    finally:
        listen_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listen_task

    # Only the well-formed message fired the callback.
    assert state_calls == [("kitchen", DeviceState.ONLINE)]


async def test_listen_processes_fresh_discover_messages() -> None:
    """A fresh (non-retained) discover message updates state and IP."""
    state_calls: list[tuple[str, DeviceState]] = []
    ip_calls: list[tuple[str, str]] = []
    seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda n, ip: ip_calls.append((n, ip)),
    )

    class _FreshMessage:
        topic = "esphome/discover/kitchen"
        payload = json.dumps({"name": "kitchen", "ip": "10.0.0.5"}).encode()
        retain = False

    queue: asyncio.Queue = asyncio.Queue()
    await queue.put(_FreshMessage())

    listen_task = asyncio.create_task(monitor._listen(queue))
    try:
        await asyncio.wait_for(seen.wait(), timeout=1.0)
    finally:
        listen_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listen_task

    assert state_calls == [("kitchen", DeviceState.ONLINE)]
    assert ip_calls == [("kitchen", "10.0.0.5")]


# ---------------------------------------------------------------------------
# DeviceMqttMonitor — start / stop / running / is_available / _ping_loop
# ---------------------------------------------------------------------------


def test_is_available_tracks_paho_module_presence() -> None:
    """``is_available`` is exactly ``paho_mqtt is not None``.

    Bidirectional contract — locks the predicate regardless of
    whether the test environment actually has paho-mqtt installed.
    The CI matrix that includes the [esphome] extra exercises the
    True branch; a stripped install (e.g. a minimal Docker image
    without the extra) running this same test would exercise the
    False branch. The ``test_is_available_false_when_paho_missing``
    test below pins the False branch unconditionally via
    monkeypatch.
    """
    expected = monitor_module.paho_mqtt is not None
    assert DeviceMqttMonitor.is_available() is expected


def test_is_available_false_when_paho_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``is_available`` returns ``False`` when paho-mqtt isn't importable.

    The dashboard ships with the import wrapped in ``try / except
    ImportError`` so a stripped install (e.g. a Docker image without
    the [esphome] extra) doesn't crash at import time. ``start()``
    consults ``is_available()`` and skips the listener with a
    helpful warning when paho is gone.
    """
    monkeypatch.setattr(monitor_module, "paho_mqtt", None)
    assert DeviceMqttMonitor.is_available() is False


async def test_running_reflects_task_state() -> None:
    """``running`` is True between ``start`` and ``stop``, False outside.

    Exposed for the coordinator's idempotency check ("is this
    monitor already up?") so a duplicate ``start`` doesn't spawn
    a second connect loop.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    assert monitor.running is False  # before start

    # Stand-in for the listener task — never resolves so the
    # monitor stays in the "running" state until we cancel it.
    parked = asyncio.Event()
    monitor._task = asyncio.create_task(parked.wait())
    try:
        assert monitor.running is True
    finally:
        monitor._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor._task

    # A done task no longer counts as running.
    assert monitor.running is False


async def test_start_warns_and_returns_when_paho_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No paho → log warning, don't spawn the listener task.

    Without this early return ``_run`` would crash on the very
    first ``paho_mqtt.Client(...)`` call. The warning is the
    user-facing breadcrumb pointing at the optional ``[esphome]``
    extra.
    """
    monkeypatch.setattr(monitor_module, "paho_mqtt", None)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    with caplog.at_level("WARNING"):
        await monitor.start()

    assert monitor._task is None
    assert any("paho-mqtt not installed" in rec.message for rec in caplog.records)


async def test_start_is_idempotent_when_already_running() -> None:
    """A second ``start`` while running is a no-op — doesn't replace the task.

    Pin the contract so a regression that always re-creates the
    task would orphan the original (which keeps holding the
    paho client + thread) and double-publish discover messages.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    parked = asyncio.Event()
    monitor._task = asyncio.create_task(parked.wait())
    original_task = monitor._task
    try:
        await monitor.start()
        assert monitor._task is original_task  # no replacement
    finally:
        monitor._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor._task


async def test_stop_cancels_task_and_clears_last_seen() -> None:
    """``stop`` cancels the runner and forgets every observation.

    Last-seen entries are paired with a live broker subscription;
    keeping them after stop would feed the next ``start`` stale
    timestamps and immediately mark the device offline (they're
    older than ``_OFFLINE_TIMEOUT``).
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    parked = asyncio.Event()
    monitor._task = asyncio.create_task(parked.wait())
    monitor._last_seen["kitchen"] = 12345.0

    await monitor.stop()

    assert monitor._task is None
    assert monitor._last_seen == {}


async def test_stop_is_no_op_when_never_started() -> None:
    """``stop`` on a never-started monitor is a clean no-op.

    Pairs with the coordinator's "drop a broker that no devices
    use" path — it calls ``stop`` unconditionally, which mustn't
    crash on a monitor that never reached ``start``.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    await monitor.stop()
    assert monitor._task is None


async def test_ping_loop_marks_stale_devices_offline_and_republishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale ``_last_seen`` entries flip OFFLINE; broker gets a re-publish each tick.

    The ping loop is the failsafe that fires when MQTT silently
    stops delivering — devices' last-seen ages past
    ``_OFFLINE_TIMEOUT`` and they switch to OFFLINE without a
    fresh subscribe-side signal. The re-publish on every tick
    pokes the broker so any device that quietly came back gets
    a chance to announce again.

    Speed up the loop by patching ``_PING_INTERVAL`` and
    ``_OFFLINE_TIMEOUT`` — the production values (2s / 10s)
    would make this test wait ten seconds for an offline flip.
    """
    # 50ms / 100ms: well under any plausible test-host scheduler
    # jitter while still letting "stale" form between ticks.
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.05)
    monkeypatch.setattr(monitor_module, "_OFFLINE_TIMEOUT", 0.1)

    state_calls: list[tuple[str, DeviceState]] = []
    offline_seen = asyncio.Event()

    def on_state(name: str, state: DeviceState) -> None:
        state_calls.append((name, state))
        if state == DeviceState.OFFLINE:
            offline_seen.set()

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=on_state,
        on_ip_change=lambda *_: None,
    )

    class _FakeClient:
        def __init__(self) -> None:
            self.publishes: list[tuple[str, Any, bool]] = []

        def publish(self, topic: str, payload: Any = None, retain: bool = False) -> None:
            self.publishes.append((topic, payload, retain))

    fake = _FakeClient()

    # Seed a stale entry that's already past the (patched) offline
    # timeout. The first tick should sweep it.
    loop = asyncio.get_running_loop()
    monitor._last_seen["ghost"] = loop.time() - 1.0

    ping_task = asyncio.create_task(monitor._ping_loop(fake))
    try:
        await asyncio.wait_for(offline_seen.wait(), timeout=2.0)
    finally:
        ping_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ping_task

    assert ("ghost", DeviceState.OFFLINE) in state_calls
    assert "ghost" not in monitor._last_seen
    # Each tick republishes the discover trigger.
    assert fake.publishes
    topic, _payload, retain = fake.publishes[0]
    assert topic == "esphome/discover"
    assert retain is False


# ---------------------------------------------------------------------------
# DeviceMqttMonitor._run — reconnect-on-error loop
# ---------------------------------------------------------------------------


async def test_start_spawns_run_task_when_paho_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first ``start()`` call actually creates the ``_run`` task.

    The ``test_start_is_idempotent_when_already_running`` case
    pre-seeds ``_task`` and asserts it isn't replaced — but the
    happy-path branch (``self._task = asyncio.create_task(self._run())``)
    was uncovered. Stub ``_run`` to a fast-resolving coroutine
    so the test doesn't actually try to talk to a broker, then
    verify ``running`` flipped True.

    Force ``paho_mqtt`` non-None for the duration of the test so
    ``start()``'s ``is_available()`` guard doesn't short-circuit
    on a stripped install (CI without the ``[esphome]`` extra,
    or a Docker base image that omits paho).
    """
    if monitor_module.paho_mqtt is None:
        # Stand-in module — only the truthiness matters here, the
        # stubbed ``_run`` never actually touches it.
        monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {}))

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    parked = asyncio.Event()

    async def _fake_run() -> None:
        await parked.wait()

    monkeypatch.setattr(monitor, "_run", _fake_run)

    await monitor.start()

    try:
        assert monitor.running is True
        assert monitor._task is not None
    finally:
        parked.set()
        await monitor.stop()
    assert monitor.running is False


async def test_connect_and_listen_subscribes_publishes_and_runs_listen_ping(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_connect_and_listen`` wires paho callbacks, subscribes, and runs the inner tasks.

    Drive the full body without a real broker by stubbing
    ``paho_mqtt.Client`` and the inner ``_listen`` / ``_ping_loop``
    coroutines. Pin: ``connect`` / ``loop_start`` / ``subscribe``
    / ``publish`` are called in order, the inner tasks fire, and
    teardown runs ``loop_stop`` + ``disconnect`` even on cancel.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local", port=1883, username="alice", password="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    calls: list[tuple[str, Any]] = []
    listen_started = asyncio.Event()
    ping_started = asyncio.Event()

    class _FakeClient:
        def __init__(self, client_id: str = "", clean_session: bool = True) -> None:
            calls.append(("init", (client_id, clean_session)))
            self.on_connect: Any = None
            self.on_message: Any = None

        def username_pw_set(self, username: str, password: str) -> None:
            calls.append(("username_pw_set", (username, password)))

        def connect(self, host: str, port: int) -> None:
            calls.append(("connect", (host, port)))

        def loop_start(self) -> None:
            calls.append(("loop_start", ()))
            # Fire on_connect with rc=0 (success) on a thread-like
            # callback. Production calls this from paho's network
            # thread via call_soon_threadsafe; here we call it
            # directly since we're already on the loop.
            self.on_connect(self, None, None, 0)
            # Fire one on_message so the inner queue-bridge
            # closure (line 166) gets exercised.
            fake_msg = type("M", (), {"topic": "x", "payload": b"", "retain": False})()
            self.on_message(self, None, fake_msg)

        def loop_stop(self) -> None:
            calls.append(("loop_stop", ()))

        def subscribe(self, topic: str) -> None:
            calls.append(("subscribe", (topic,)))

        def publish(self, topic: str, payload: Any = None, retain: bool = False) -> None:
            calls.append(("publish", (topic, payload, retain)))

        def disconnect(self) -> None:
            calls.append(("disconnect", ()))

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    async def _fake_listen(_queue: Any) -> None:
        listen_started.set()
        await asyncio.Event().wait()

    async def _fake_ping(_client: Any) -> None:
        ping_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(monitor, "_listen", _fake_listen)
    monkeypatch.setattr(monitor, "_ping_loop", _fake_ping)

    task = asyncio.create_task(monitor._connect_and_listen("test-id"))
    try:
        await asyncio.wait_for(listen_started.wait(), timeout=2.0)
        await asyncio.wait_for(ping_started.wait(), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    op_names = [c[0] for c in calls]
    # Ordered: init → username/pw → connect → loop_start → subscribe
    # → publish → loop_stop → disconnect.
    assert op_names == [
        "init",
        "username_pw_set",
        "connect",
        "loop_start",
        "subscribe",
        "publish",
        "loop_stop",
        "disconnect",
    ]
    # Subscribe goes against the discover wildcard; publish kicks
    # the broker for an immediate announce.
    assert ("subscribe", ("esphome/discover/#",)) in calls
    publishes = [c for c in calls if c[0] == "publish"]
    assert publishes == [("publish", ("esphome/discover", None, False))]


async def test_connect_and_listen_raises_on_broker_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero CONNACK rc raises ``ConnectionError`` so ``_run`` retries.

    The retry path (``test_run_reconnects_on_connect_and_listen_failure``)
    proves the loop catches the error; this test pins that the
    error is actually raised when paho reports a rejected connect.
    Disconnect + loop_stop must still run in the finally block.
    """
    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="broker.local"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    teardown_calls: list[str] = []

    class _FakeClient:
        def __init__(self, client_id: str = "", clean_session: bool = True) -> None:
            self.on_connect: Any = None
            self.on_message: Any = None

        def connect(self, host: str, port: int) -> None:
            return None

        def loop_start(self) -> None:
            # rc=4 == "bad username/password" — any non-zero rejects.
            self.on_connect(self, None, None, 4)

        def loop_stop(self) -> None:
            teardown_calls.append("loop_stop")

        def subscribe(self, topic: str) -> None:
            return None

        def publish(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def disconnect(self) -> None:
            teardown_calls.append("disconnect")

    monkeypatch.setattr(monitor_module, "paho_mqtt", type("M", (), {"Client": _FakeClient}))

    with pytest.raises(ConnectionError, match="rc=4"):
        await monitor._connect_and_listen("test-id")

    # Teardown ran even though we raised.
    assert teardown_calls == ["loop_stop", "disconnect"]


async def test_run_reconnects_on_connect_and_listen_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broker error in ``_connect_and_listen`` triggers a delayed retry.

    ``_run``'s reconnect loop is what survives transient broker
    blips (network glitch, broker restart). A bare exception
    inside ``_connect_and_listen`` would otherwise kill the
    monitor permanently. The test patches the underlying
    coroutine to raise once, then succeed — and asserts the
    second call happened.

    Speed up via ``_RECONNECT_DELAY = 0`` so the test doesn't
    wait the production 5s between attempts.
    """
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )
    # Seed last_seen so we can verify it gets cleared on error
    # (production keeps device state alone — only ``_last_seen``
    # is reset — so a brief blip doesn't trigger an offline storm).
    monitor._last_seen["kitchen"] = 0.0

    call_count = 0
    second_call = asyncio.Event()

    async def _fake_connect(_client_id: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            msg = "broker rejected"
            raise ConnectionError(msg)
        second_call.set()
        # Park to keep the runner alive until cancelled.
        await asyncio.Event().wait()

    monkeypatch.setattr(monitor, "_connect_and_listen", _fake_connect)

    run_task = asyncio.create_task(monitor._run())
    try:
        await asyncio.wait_for(second_call.wait(), timeout=2.0)
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

    assert call_count >= 2
    # First-attempt error cleared last_seen — pin the contract
    # so a regression that leaves stale entries (which would
    # then immediately mark the device offline on the next ping
    # tick) surfaces here.
    assert monitor._last_seen == {}


async def test_run_collapses_repeat_unreachable_errors_to_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeat unreachable-broker errors stay at DEBUG, not ERROR+traceback.

    When the broker is offline for a long time the reconnect loop
    fires every ``_RECONNECT_DELAY`` seconds. Logging a full ERROR
    with traceback on each tick floods journalctl / Home Assistant's
    log view (issue #324). The first failure should still be loud
    (WARNING, no traceback for expected ``TimeoutError`` /
    ``OSError`` / ``ConnectionError``) so the operator sees the
    broker went away; subsequent identical failures collapse to
    DEBUG so the file doesn't fill with copies of the same trace.
    """
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    call_count = 0
    third_call = asyncio.Event()

    async def _always_timeout(_client_id: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            third_call.set()
        raise TimeoutError("timed out")

    monkeypatch.setattr(monitor, "_connect_and_listen", _always_timeout)

    caplog.set_level("DEBUG", logger=monitor_module.__name__)

    run_task = asyncio.create_task(monitor._run())
    try:
        await asyncio.wait_for(third_call.wait(), timeout=2.0)
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

    unreachable = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__ and "unreachable" in r.message
    ]
    # Exactly one WARNING for the first transition into "unreachable",
    # the rest collapsed to DEBUG. ``exc_info`` must be None on every
    # such record — pin that there's no traceback being attached.
    warnings = [r for r in unreachable if r.levelname == "WARNING"]
    debugs = [r for r in unreachable if r.levelname == "DEBUG"]
    assert len(warnings) == 1, [r.levelname for r in unreachable]
    assert len(debugs) >= 1
    for record in unreachable:
        assert record.exc_info is None


async def test_run_resets_log_gate_after_successful_connect(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful CONNACK re-arms the loud-warning gate.

    Without the reset, a broker that goes down → up → down again
    would only WARN once (on the very first failure) and silently
    DEBUG every subsequent outage forever, defeating the point of
    surfacing it in the operator's log. The reset trigger is
    ``self._connected_this_session = True`` (set inside
    ``_connect_and_listen`` right after CONNACK), not a clean
    return — production almost never sees a clean return because
    the inner TaskGroup parks until cancelled or raises.
    """
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    # Sequence of behaviours per ``_connect_and_listen`` call:
    # 1. fail (TimeoutError) — first WARNING
    # 2. simulate a session that reached CONNACK and then was
    #    closed by the broker (sets the in-session flag, then
    #    raises an expected error). Production's equivalent is a
    #    broker that accepted the connection, ran for a while, and
    #    then dropped us — the gate must re-arm.
    # 3. fail (TimeoutError) — should be a *second* WARNING, not DEBUG
    behaviours = ["fail", "connect-then-drop", "fail"]
    third_failure = asyncio.Event()

    async def _scripted(_client_id: str) -> None:
        if not behaviours:
            third_failure.set()
            await asyncio.Event().wait()
        action = behaviours.pop(0)
        if not behaviours:
            third_failure.set()
        if action == "fail":
            raise TimeoutError("timed out")
        # ``connect-then-drop``: signal CONNACK success, then
        # raise as if the broker dropped the session.
        monitor._connected_this_session = True
        raise ConnectionError("broker dropped session")

    monkeypatch.setattr(monitor, "_connect_and_listen", _scripted)

    caplog.set_level("DEBUG", logger=monitor_module.__name__)

    run_task = asyncio.create_task(monitor._run())
    try:
        await asyncio.wait_for(third_failure.wait(), timeout=2.0)
        # Give the loop one extra tick to log the third failure
        # before we tear it down.
        await asyncio.sleep(0.05)
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

    warnings = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__
        and r.levelname == "WARNING"
        and "unreachable" in r.message
    ]
    # Two WARNINGs: the first failure (start of outage A) and the
    # connect-then-drop (start of outage B — gate re-armed by the
    # CONNACK in between). The third failure is a continuation of
    # outage B with no successful connect between them, so it
    # collapses to DEBUG. Pinning this also catches the inverse
    # regression: dropping the gate-reset entirely would only emit
    # one WARNING here instead of two.
    assert len(warnings) == 2, [r.message for r in warnings]
    debugs = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__
        and r.levelname == "DEBUG"
        and "unreachable" in r.message
    ]
    assert len(debugs) == 1, [r.message for r in debugs]


async def test_run_loud_logs_unexpected_after_expected_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A new unexpected exception after a connect-error loop still logs ERROR+traceback.

    The two log gates (expected vs unexpected) are tracked
    separately so a long ``TimeoutError`` outage can't suppress
    the first appearance of an *unexpected* exception class —
    that would hide a genuine bug behind the offline-broker
    spam-suppression. Pin: after a TimeoutError WARNING, a
    subsequent ``RuntimeError`` (unrelated category) logs at
    ERROR level with traceback (``exc_info``) attached.
    """
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    # First call raises TimeoutError (expected, WARNING),
    # second call raises RuntimeError (unexpected, must be loud).
    behaviours: list[str] = ["timeout", "unexpected"]
    second_call = asyncio.Event()

    async def _scripted(_client_id: str) -> None:
        if not behaviours:
            second_call.set()
            await asyncio.Event().wait()
        action = behaviours.pop(0)
        if not behaviours:
            second_call.set()
        if action == "timeout":
            raise TimeoutError("timed out")
        msg = "kaboom"
        raise RuntimeError(msg)

    monkeypatch.setattr(monitor, "_connect_and_listen", _scripted)

    caplog.set_level("DEBUG", logger=monitor_module.__name__)

    run_task = asyncio.create_task(monitor._run())
    try:
        await asyncio.wait_for(second_call.wait(), timeout=2.0)
        await asyncio.sleep(0.05)
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

    errors = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__ and r.levelname == "ERROR" and "error" in r.message
    ]
    assert len(errors) == 1, [(r.levelname, r.message) for r in errors]
    # ``logger.exception`` attaches exc_info — pin the traceback is
    # actually present so a regression that drops the exception
    # context (or routes through DEBUG) surfaces here.
    assert errors[0].exc_info is not None


async def test_run_collapses_repeat_unexpected_errors_to_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeat unexpected exceptions log DEBUG with class+message, no traceback.

    Covers the suppressed-traceback DEBUG branch in
    ``_log_reconnect_failure``'s unexpected-error path. The first
    occurrence still emits one ERROR with traceback (proven in
    ``test_run_loud_logs_unexpected_after_expected_failure``);
    every subsequent occurrence with the gate already tripped
    falls back to DEBUG so a tight failure loop doesn't dump the
    same trace into the log every ``_RECONNECT_DELAY``. Pin: the
    DEBUG line still includes the exception class name and message
    so the operator can tell what's repeating without raising the
    log level back to ERROR.
    """
    monkeypatch.setattr(monitor_module, "_RECONNECT_DELAY", 0)

    monitor = DeviceMqttMonitor(
        broker=MqttBrokerConfig(host="x"),
        on_state_change=lambda *_: None,
        on_ip_change=lambda *_: None,
    )

    call_count = 0
    third_call = asyncio.Event()

    async def _always_runtime_error(_client_id: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            third_call.set()
        msg = "kaboom"
        raise RuntimeError(msg)

    monkeypatch.setattr(monitor, "_connect_and_listen", _always_runtime_error)

    caplog.set_level("DEBUG", logger=monitor_module.__name__)

    run_task = asyncio.create_task(monitor._run())
    try:
        await asyncio.wait_for(third_call.wait(), timeout=2.0)
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

    errors = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__ and r.levelname == "ERROR" and "error" in r.message
    ]
    debugs = [
        r
        for r in caplog.records
        if r.name == monitor_module.__name__
        and r.levelname == "DEBUG"
        and "suppressed traceback" in r.message
    ]
    # Exactly one ERROR (first hit) and at least one DEBUG-suppressed
    # follow-up — the repeats. Anything more than one ERROR means the
    # gate didn't trip; zero DEBUG means the suppressed branch was
    # never reached.
    assert len(errors) == 1, [(r.levelname, r.message) for r in errors]
    assert len(debugs) >= 1
    # Every DEBUG must include the exception class + message — that's
    # the whole point of capturing ``as err`` in the broad branch.
    for record in debugs:
        assert "RuntimeError" in record.message
        assert "kaboom" in record.message
        # And no traceback should be attached at this level — the
        # promise of "suppressed traceback" is meaningful only if it
        # actually drops the exc_info too.
        assert record.exc_info is None


# ---------------------------------------------------------------------------
# Pure helpers — _extract_ip / _decode_payload
# ---------------------------------------------------------------------------


def test_extract_ip_returns_first_present_address() -> None:
    """``_extract_ip`` returns the first ``ip``/``ip0``/``ip1``/``ip2`` set.

    Some ESPHome firmwares expose multiple IPs (Wi-Fi + Ethernet,
    Wi-Fi + AP). The dashboard only needs one to dial back; the
    first is the canonical primary, secondaries are fallbacks
    when it's unreachable. Pin the iteration order ``ip`` →
    ``ip0`` → ``ip1`` → ``ip2`` so a regression that flips it
    surfaces here.
    """
    # ``ip`` wins when present.
    assert _extract_ip({"ip": "10.0.0.1", "ip0": "192.168.1.1", "ip1": "172.16.0.1"}) == "10.0.0.1"
    # Falls through to ``ip0`` when ``ip`` missing.
    assert _extract_ip({"ip0": "192.168.1.1", "ip1": "172.16.0.1"}) == "192.168.1.1"
    # And to ``ip1`` / ``ip2`` in turn.
    assert _extract_ip({"ip1": "172.16.0.1", "ip2": "10.10.10.10"}) == "172.16.0.1"
    assert _extract_ip({"ip2": "10.10.10.10"}) == "10.10.10.10"


def test_extract_ip_skips_empty_and_non_string_values() -> None:
    """Empty strings / non-strings are skipped; missing all → ``""``.

    Defensive: a misbehaving firmware that publishes ``"ip": null``
    or ``"ip": ""`` shouldn't shadow the next address candidate.
    """
    # Empty + non-string ``ip`` skipped, falls through to ``ip1``.
    assert _extract_ip({"ip": "", "ip0": None, "ip1": "172.16.0.1"}) == "172.16.0.1"
    # Numeric-shaped non-string skipped (devices shouldn't do this
    # but the helper guards against it anyway).
    assert _extract_ip({"ip": 12345}) == ""
    # Nothing present at all.
    assert _extract_ip({}) == ""
    assert _extract_ip({"name": "kitchen", "version": "2026.5.0"}) == ""


def test_decode_payload_handles_str_bytes_and_garbage() -> None:
    """``_decode_payload`` accepts ``str`` / ``bytes`` / ``bytearray`` / ``memoryview``.

    paho-mqtt's payload type isn't strictly typed at the wire —
    the helper has to tolerate every shape paho might produce.
    Malformed UTF-8 falls back to ``backslashreplace`` so the
    debug log line stays readable.
    """
    assert _decode_payload("already-text") == "already-text"
    assert _decode_payload(b"raw bytes") == "raw bytes"
    assert _decode_payload(bytearray(b"mutable")) == "mutable"
    assert _decode_payload(memoryview(b"viewed")) == "viewed"
    # Malformed UTF-8: the leading 0x80 isn't a valid start byte;
    # ``backslashreplace`` keeps it visible without raising.
    decoded = _decode_payload(b"\x80hello")
    assert "hello" in decoded


def test_decode_payload_returns_empty_for_unsupported_types() -> None:
    """``None`` and other unsupported payload shapes return ``""``.

    The caller guards against a falsy return so an empty string
    safely short-circuits the JSON parse without raising.
    """
    assert _decode_payload(None) == ""
    assert _decode_payload(12345) == ""
    assert _decode_payload({"not": "supported"}) == ""
    assert _decode_payload(["nope"]) == ""
