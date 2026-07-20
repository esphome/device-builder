"""
End-to-end MQTT discovery against a real broker.

Six fake devices split across two broker logins, driven through the
real coordinator/monitor stack: subscriber-gated broadcasts, one
elected broadcaster per broker, online detection, and offline aging.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("amqtt.broker")
import paho.mqtt.client as paho
from amqtt.broker import Broker
from passlib.apps import custom_app_context

from esphome_device_builder.controllers import _device_mqtt_monitor as monitor_module
from esphome_device_builder.controllers._device_mqtt_coordinator import DeviceMqttCoordinator
from esphome_device_builder.helpers.subscriber_presence import SubscriberPresence
from esphome_device_builder.models import Device, DeviceState

_LOGINS = {"alpha": "pwA", "beta": "pwB"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for(condition: Callable[[], bool], timeout: float, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


class _FakeMqttDevice:
    """Threaded paho client that answers each discover broadcast."""

    def __init__(self, name: str, ip: str, port: int, username: str, password: str) -> None:
        self.name = name
        self.answering = True
        self._payload = json.dumps({"name": name, "ip": ip})
        self._client = paho.Client(client_id=f"fake-{name}")
        self._client.username_pw_set(username, password)
        self._client.on_connect = lambda c, _u, _f, _rc: c.subscribe("esphome/discover")
        self._client.on_message = self._on_message
        self._client.connect_async("127.0.0.1", port)
        self._client.loop_start()

    def _on_message(self, client: Any, _userdata: Any, _msg: Any) -> None:
        if self.answering:
            client.publish(f"esphome/discover/{self.name}", self._payload)

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


class _BroadcastProbe:
    """Anonymous client counting ``esphome/discover`` broadcasts."""

    def __init__(self, port: int) -> None:
        self._seen: list[float] = []
        self._client = paho.Client(client_id="probe")
        self._client.on_connect = lambda c, _u, _f, _rc: c.subscribe("esphome/discover")
        self._client.on_message = lambda _c, _u, _m: self._seen.append(0.0)
        self._client.connect_async("127.0.0.1", port)
        self._client.loop_start()

    @property
    def count(self) -> int:
        return len(self._seen)

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


@pytest.mark.timeout(60)
async def test_six_devices_two_logins_single_broadcaster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.25)
    monkeypatch.setattr(monitor_module, "_OFFLINE_TIMEOUT", 1.5)

    port = _free_port()
    passwd = tmp_path / "broker-passwd"
    passwd.write_text(
        "".join(f"{user}:{custom_app_context.hash(pw)}\n" for user, pw in _LOGINS.items())
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    broker = Broker(
        {
            "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
            "sys_interval": 0,
            "auth": {"allow-anonymous": True, "password-file": str(passwd)},
            "topic-check": {"enabled": False},
        }
    )
    await broker.start()

    devices: list[Device] = []
    fakes: list[_FakeMqttDevice] = []
    for i in range(1, 7):
        name = f"dev{i}"
        user = "alpha" if i <= 3 else "beta"
        (config_dir / f"{name}.yaml").write_text(
            f"esphome:\n  name: {name}\n"
            f"mqtt:\n  broker: 127.0.0.1\n  port: {port}\n"
            f"  username: {user}\n  password: {_LOGINS[user]}\n"
        )
        devices.append(
            Device(name=name, friendly_name=name, configuration=f"{name}.yaml", uses_mqtt=True)
        )
        fakes.append(_FakeMqttDevice(name, f"10.0.0.{i}", port, user, _LOGINS[user]))
    probe = _BroadcastProbe(port)

    states: dict[str, DeviceState] = {}
    ips: dict[str, str] = {}
    presence = SubscriberPresence()
    coord = DeviceMqttCoordinator(
        config_dir=config_dir,
        get_devices=lambda: devices,
        on_state_change=states.__setitem__,
        on_ip_change=ips.__setitem__,
        presence=presence,
    )

    try:
        await coord.reconcile()
        assert coord.active_brokers == 2
        monitors = list(coord._monitors.values())
        await _wait_for(lambda: all(m.connected for m in monitors), 10.0, "both logins connected")

        # Idle: connected and subscribed, but not a single broadcast.
        await asyncio.sleep(1.0)
        assert probe.count == 0
        assert states == {}

        with presence.subscriber():
            await _wait_for(
                lambda: all(states.get(f"dev{i}") == DeviceState.ONLINE for i in range(1, 7)),
                10.0,
                "all six devices ONLINE",
            )
            assert ips["dev1"] == "10.0.0.1"
            # One elected broadcaster per broker — two logins must not
            # double the broadcast rate.
            assert sum(m.is_publisher for m in monitors) == 1
            start_count = probe.count
            await asyncio.sleep(1.0)
            assert probe.count - start_count <= 6  # ~4 at 0.25s; dual publishers would be ~8

            # A device that stops answering ages out OFFLINE.
            fakes[0].answering = False
            await _wait_for(lambda: states.get("dev1") == DeviceState.OFFLINE, 10.0, "dev1 OFFLINE")

        # Dashboard gone: broadcasts stop again (allow one in-flight tick).
        await asyncio.sleep(0.5)
        idle_count = probe.count
        await asyncio.sleep(1.0)
        assert probe.count <= idle_count + 1
    finally:
        await coord.stop()
        for fake in fakes:
            fake.stop()
        probe.stop()
        await broker.shutdown()
