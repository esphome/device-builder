"""Shared mosquitto harness for the MQTT e2e modules."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest

paho = pytest.importorskip("paho.mqtt.client")

# brew puts the broker in sbin, which is often off PATH.
_EXTRA_PATH = "/opt/homebrew/sbin:/usr/local/sbin:/usr/sbin"
MOSQUITTO = shutil.which("mosquitto", path=None) or shutil.which("mosquitto", path=_EXTRA_PATH)
MOSQUITTO_PASSWD = shutil.which("mosquitto_passwd") or shutil.which(
    "mosquitto_passwd", path=_EXTRA_PATH
)

requires_mosquitto = pytest.mark.skipif(
    not (MOSQUITTO and MOSQUITTO_PASSWD), reason="mosquitto not installed"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def restart_mosquitto(tmp_path: Path, port: int) -> asyncio.subprocess.Process:
    """Start mosquitto against ``tmp_path/mosquitto.conf`` and wait for *port*."""
    broker = await asyncio.create_subprocess_exec(
        str(MOSQUITTO),
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


async def stop_broker(broker: asyncio.subprocess.Process) -> None:
    broker.terminate()
    with contextlib.suppress(ProcessLookupError):
        await broker.wait()


class FakeMqttDevice:
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


class BroadcastProbe:
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
