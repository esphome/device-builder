"""
End-to-end MQTT discovery against a real mosquitto broker.

Six fake paho devices split across two broker logins, driven through
the real coordinator/monitor stack: subscriber-gated broadcasts, one
elected broadcaster per broker, online detection, and offline aging.
Skips when the mosquitto binaries aren't installed; the dedicated
linux ``e2e-mqtt`` CI job installs them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from esphome_device_builder.controllers import _device_mqtt_monitor as monitor_module
from esphome_device_builder.controllers._device_mqtt_coordinator import DeviceMqttCoordinator
from esphome_device_builder.helpers.subscriber_presence import SubscriberPresence
from esphome_device_builder.models import Device, DeviceState

paho = pytest.importorskip("paho.mqtt.client")

# brew puts the broker in sbin, which is often off PATH.
_EXTRA_PATH = "/opt/homebrew/sbin:/usr/local/sbin:/usr/sbin"
_MOSQUITTO = shutil.which("mosquitto", path=None) or shutil.which("mosquitto", path=_EXTRA_PATH)
_MOSQUITTO_PASSWD = shutil.which("mosquitto_passwd") or shutil.which(
    "mosquitto_passwd", path=_EXTRA_PATH
)

pytestmark = pytest.mark.skipif(
    not (_MOSQUITTO and _MOSQUITTO_PASSWD), reason="mosquitto not installed"
)

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


async def _restart_mosquitto(tmp_path: Path, port: int) -> asyncio.subprocess.Process:
    broker = await asyncio.create_subprocess_exec(
        str(_MOSQUITTO),
        "-c",
        str(tmp_path / "mosquitto.conf"),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    deadline = asyncio.get_running_loop().time() + 10.0
    while True:
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if asyncio.get_running_loop().time() > deadline:
                broker.terminate()
                pytest.fail("mosquitto did not come back after restart")
            await asyncio.sleep(0.05)
        else:
            writer.close()
            await writer.wait_closed()
            return broker


async def _start_mosquitto(tmp_path: Path) -> tuple[asyncio.subprocess.Process, int]:
    passwd = tmp_path / "mosquitto-passwd"
    for index, (user, password) in enumerate(_LOGINS.items()):
        create_flag = ["-c"] if index == 0 else []
        proc = await asyncio.create_subprocess_exec(
            str(_MOSQUITTO_PASSWD), *create_flag, "-b", str(passwd), user, password
        )
        assert await proc.wait() == 0

    port = _free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\npassword_file {passwd}\n")
    return await _restart_mosquitto(tmp_path, port), port


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
async def test_broker_restart_resubscribes_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broker restart must not flip devices OFFLINE, and discovery must resume.

    Pins the resubscribe-on-reconnect fix against real paho
    auto-reconnect rather than a hand-fired callback.
    """
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.25)
    # Generous timeout: the outage itself must not age anyone out; the
    # aging behaviour is pinned by the other test.
    monkeypatch.setattr(monitor_module, "_OFFLINE_TIMEOUT", 30.0)

    broker, port = await _start_mosquitto(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    devices: list[Device] = []
    fakes: list[_FakeMqttDevice] = []
    for i in (1, 2):
        name = f"dev{i}"
        (config_dir / f"{name}.yaml").write_text(
            f"esphome:\n  name: {name}\n"
            f"mqtt:\n  broker: 127.0.0.1\n  port: {port}\n"
            f"  username: alpha\n  password: {_LOGINS['alpha']}\n"
        )
        devices.append(
            Device(name=name, friendly_name=name, configuration=f"{name}.yaml", uses_mqtt=True)
        )
        fakes.append(_FakeMqttDevice(name, f"10.0.1.{i}", port, "alpha", _LOGINS["alpha"]))

    events: list[tuple[str, DeviceState]] = []
    presence = SubscriberPresence()
    coord = DeviceMqttCoordinator(
        config_dir=config_dir,
        get_devices=lambda: devices,
        on_state_change=lambda n, s: events.append((n, s)),
        on_ip_change=lambda *_: None,
        presence=presence,
    )

    try:
        await coord.reconcile()
        with presence.subscriber():
            await _wait_for(
                lambda: {n for n, s in events if s == DeviceState.ONLINE} == {"dev1", "dev2"},
                10.0,
                "both devices ONLINE",
            )

            broker.terminate()
            with contextlib.suppress(ProcessLookupError):
                await broker.wait()
            await asyncio.sleep(1.0)

            # Same port so paho's auto-reconnect finds the new broker.
            broker = await _restart_mosquitto(tmp_path, port)

            events.clear()
            await _wait_for(
                lambda: {n for n, s in events if s == DeviceState.ONLINE} == {"dev1", "dev2"},
                20.0,
                "devices rediscovered after broker restart",
            )
        assert not [e for e in events if e[1] == DeviceState.OFFLINE], (
            "broker outage must not flip devices OFFLINE"
        )
    finally:
        await coord.stop()
        for fake in fakes:
            fake.stop()
        broker.terminate()
        with contextlib.suppress(ProcessLookupError):
            await broker.wait()


@pytest.mark.timeout(60)
async def test_six_devices_two_logins_single_broadcaster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.25)
    monkeypatch.setattr(monitor_module, "_OFFLINE_TIMEOUT", 1.5)

    broker, port = await _start_mosquitto(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()

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
        broker.terminate()
        with contextlib.suppress(ProcessLookupError):
            await broker.wait()
