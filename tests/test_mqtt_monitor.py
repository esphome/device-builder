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

import pytest

from esphome_device_builder.controllers import _device_mqtt_monitor as monitor_module
from esphome_device_builder.controllers._device_mqtt_coordinator import (
    DeviceMqttCoordinator,
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
    yaml = "mqtt:\n  broker: !secret broker_host\n  password: !secret pw\n"
    secrets = {"broker_host": "192.168.1.5", "pw": "topsecret"}
    config = parse_mqtt_block(yaml, secrets)
    assert config is not None
    assert config.host == "192.168.1.5"
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
