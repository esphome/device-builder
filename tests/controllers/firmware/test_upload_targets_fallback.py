"""Contract pin for the local ``esphome.upload_targets`` fallback."""

# TODO: delete with _upload_targets_fallback.

from __future__ import annotations

import pytest

from esphome_device_builder.controllers.firmware._upload_targets_fallback import (
    PortType,
    get_port_type,
)


@pytest.mark.parametrize(
    ("port", "expected"),
    [
        ("BOOTSEL", PortType.BOOTSEL),
        ("MQTT", PortType.MQTT),
        ("MQTTIP", PortType.MQTTIP),
        ("/dev/ttyUSB0", PortType.SERIAL),
        ("COM3", PortType.SERIAL),
        ("kitchen.local", PortType.NETWORK),
        ("192.168.1.42", PortType.NETWORK),
        ("ttyUSB0", PortType.NETWORK),  # bare name — no ``/`` or ``COM`` prefix
    ],
)
def test_get_port_type(port: str, expected: PortType) -> None:
    assert get_port_type(port) is expected
