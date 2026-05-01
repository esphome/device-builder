"""
Tests for MQTT detection and the async MQTT status monitor.

Covers the parts that don't require a live broker:
* YAML parsing for the ``mqtt:`` opt-in (helpers.device_yaml)
* Lifecycle gating in ``DeviceMqttMonitor`` (env / aiomqtt / running flag)
* Source-priority logic in ``DeviceStateMonitor`` (mdns > mqtt > ping)
"""

from __future__ import annotations

import asyncio

import pytest

from esphome_device_builder.controllers._device_mqtt_monitor import DeviceMqttMonitor
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
# DeviceMqttMonitor — gating
# ---------------------------------------------------------------------------


def _noop_state(_name: str, _state: DeviceState) -> None:
    return None


def _noop_ip(_name: str, _ip: str) -> None:
    return None


def _make_monitor(devices: list[Device] | None = None) -> DeviceMqttMonitor:
    devices = devices or []
    return DeviceMqttMonitor(
        get_devices=lambda: devices,
        on_state_change=_noop_state,
        on_ip_change=_noop_ip,
    )


async def test_monitor_skips_start_when_broker_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ESPHOME_DASHBOARD_MQTT_BROKER", raising=False)
    monitor = _make_monitor()
    await monitor.start()
    assert monitor.running is False


async def test_monitor_idempotent_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ESPHOME_DASHBOARD_MQTT_BROKER", raising=False)
    monitor = _make_monitor()
    await monitor.start()
    await monitor.start()  # second call must not raise
    await monitor.stop()


async def test_monitor_stop_without_start_is_noop() -> None:
    monitor = _make_monitor()
    await monitor.stop()  # must not raise even though never started
    assert monitor.running is False


def test_monitor_is_configured_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESPHOME_DASHBOARD_MQTT_BROKER", "broker.example")
    assert DeviceMqttMonitor.is_configured() is True
    monkeypatch.setenv("ESPHOME_DASHBOARD_MQTT_BROKER", "   ")
    assert DeviceMqttMonitor.is_configured() is False


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


# ---------------------------------------------------------------------------
# Lifecycle smoke — verify ``stop()`` cancels a running task even when the
# broker is wedged. Uses an aiomqtt.Client stub to avoid network IO.
# ---------------------------------------------------------------------------


class _StubMessages:
    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    def __aiter__(self) -> _StubMessages:
        return self

    async def __anext__(self) -> object:
        await self._gate.wait()
        raise StopAsyncIteration


class _StubClient:
    """Minimal aiomqtt.Client lookalike that blocks until cancelled."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self._block = asyncio.Event()

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def subscribe(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def publish(self, *_args: object, **_kwargs: object) -> None:
        return None

    @property
    def messages(self) -> _StubMessages:
        return _StubMessages(self._block)


class _FakeAiomqtt:
    """Stand-in for the aiomqtt module."""

    Client = _StubClient

    class MqttError(Exception):
        pass


async def test_monitor_stop_cancels_running_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESPHOME_DASHBOARD_MQTT_BROKER", "broker.example")
    monitor = _make_monitor()

    import esphome_device_builder.controllers._device_mqtt_monitor as mod

    monkeypatch.setattr(mod, "aiomqtt", _FakeAiomqtt(), raising=True)

    await monitor.start()
    assert monitor.running is True
    # Yield twice so the connect task reaches the listen loop before stop().
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await monitor.stop()
    assert monitor.running is False
