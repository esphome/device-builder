"""
End-to-end MQTT discovery against a mosquitto TLS listener.

Real paho TLS handshakes against an openssl-generated cert; the fake
devices answer on a sibling plaintext listener of the same broker.
Skips when mosquitto or openssl aren't installed.
"""

from __future__ import annotations

import asyncio
import shutil
import textwrap
from pathlib import Path

import pytest

from esphome_device_builder.controllers import _device_mqtt_monitor as monitor_module
from esphome_device_builder.controllers._device_mqtt_coordinator import DeviceMqttCoordinator
from esphome_device_builder.helpers.subprocess import run_subprocess_capture
from esphome_device_builder.helpers.subscriber_presence import SubscriberPresence
from esphome_device_builder.models import Device, DeviceState

from ....conftest import wait_until
from .conftest import (
    FakeMqttDevice,
    free_port,
    requires_mosquitto,
    restart_mosquitto,
    stop_broker,
)

_OPENSSL = shutil.which("openssl")

pytestmark = [
    requires_mosquitto,
    pytest.mark.skipif(not _OPENSSL, reason="openssl not installed"),
]


async def _make_cert(tmp_path: Path, san: str) -> str:
    """Generate a self-signed server cert for *san*; return its PEM (the CA)."""
    result = await run_subprocess_capture(
        str(_OPENSSL),
        "req",
        "-x509",
        "-newkey",
        "ec",
        "-pkeyopt",
        "ec_paramgen_curve:prime256v1",
        "-nodes",
        "-keyout",
        str(tmp_path / "server.key"),
        "-out",
        str(tmp_path / "server.pem"),
        "-days",
        "2",
        "-subj",
        "/CN=mosquitto",
        "-addext",
        f"subjectAltName={san}",
        timeout=30.0,
    )
    assert result.returncode == 0, result.stdout
    return (tmp_path / "server.pem").read_text()


async def _start_tls_mosquitto(
    tmp_path: Path,
) -> tuple[asyncio.subprocess.Process, int, int]:
    """Start mosquitto with a plaintext and a TLS listener; return (proc, plain, tls)."""
    plain_port, tls_port = free_port(), free_port()
    (tmp_path / "mosquitto.conf").write_text(
        "per_listener_settings false\n"
        "allow_anonymous true\n"
        f"listener {plain_port} 127.0.0.1\n"
        f"listener {tls_port} 127.0.0.1\n"
        f"certfile {tmp_path / 'server.pem'}\n"
        f"keyfile {tmp_path / 'server.key'}\n"
    )
    broker = await restart_mosquitto(tmp_path, plain_port)
    return broker, plain_port, tls_port


def _write_tls_device(
    config_dir: Path,
    name: str,
    tls_port: int,
    ca_pem: str,
    *,
    skip_cn: bool = False,
) -> Device:
    skip_line = "  skip_cert_cn_check: true\n" if skip_cn else ""
    (config_dir / f"{name}.yaml").write_text(
        f"esphome:\n  name: {name}\n"
        f"mqtt:\n  broker: 127.0.0.1\n  port: {tls_port}\n"
        f"  certificate_authority: |\n{textwrap.indent(ca_pem, '    ')}"
        f"{skip_line}"
    )
    return Device(name=name, friendly_name=name, configuration=f"{name}.yaml", uses_mqtt=True)


def _make_coordinator(
    config_dir: Path,
    devices: list[Device],
    states: dict[str, DeviceState],
    presence: SubscriberPresence,
) -> DeviceMqttCoordinator:
    return DeviceMqttCoordinator(
        config_dir=config_dir,
        get_devices=lambda: devices,
        on_state_change=states.__setitem__,
        on_ip_change=lambda *_: None,
        presence=presence,
    )


@pytest.mark.timeout(60)
async def test_tls_broker_discovery_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coordinator reaches a TLS listener via an inline CA and discovers devices."""
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.25)
    ca_pem = await _make_cert(tmp_path, "IP:127.0.0.1")
    broker, plain_port, tls_port = await _start_tls_mosquitto(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    devices = [_write_tls_device(config_dir, "dev1", tls_port, ca_pem)]
    fake = FakeMqttDevice("dev1", "10.0.2.1", plain_port, "tlsdev", "x")
    states: dict[str, DeviceState] = {}
    presence = SubscriberPresence()
    coord = _make_coordinator(config_dir, devices, states, presence)

    try:
        await coord.reconcile()
        assert coord.active_brokers == 1
        with presence.subscriber():
            await wait_until(
                lambda: states.get("dev1") == DeviceState.ONLINE,
                20.0,
                "dev1 ONLINE over the TLS session",
                interval=0.05,
            )
    finally:
        await coord.stop()
        fake.stop()
        await stop_broker(broker)


@pytest.mark.timeout(60)
async def test_tls_hostname_mismatch_requires_skip_cn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong-SAN server cert only works with ``skip_cert_cn_check`` (chain still verified)."""
    monkeypatch.setattr(monitor_module, "_PING_INTERVAL", 0.25)
    ca_pem = await _make_cert(tmp_path, "IP:10.9.9.9")
    broker, plain_port, tls_port = await _start_tls_mosquitto(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    devices = [
        _write_tls_device(config_dir, "strict", tls_port, ca_pem),
        _write_tls_device(config_dir, "lenient", tls_port, ca_pem, skip_cn=True),
    ]
    fake = FakeMqttDevice("lenient", "10.0.3.2", plain_port, "tlsdev", "x")
    states: dict[str, DeviceState] = {}
    presence = SubscriberPresence()
    coord = _make_coordinator(config_dir, devices, states, presence)

    try:
        await coord.reconcile()
        # Two broker keys: hostname-verified and skip-CN sessions.
        assert coord.active_brokers == 2
        with presence.subscriber():
            await wait_until(
                lambda: states.get("lenient") == DeviceState.ONLINE,
                20.0,
                "lenient device ONLINE via skip_cert_cn_check",
                interval=0.05,
            )
        strict_monitor = next(
            m for m in coord._monitors.values() if not m._broker.skip_cert_cn_check
        )
        assert strict_monitor.connected is False
    finally:
        await coord.stop()
        fake.stop()
        await stop_broker(broker)
