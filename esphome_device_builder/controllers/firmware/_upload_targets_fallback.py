"""Local mirror of ``esphome.upload_targets`` for older esphome installs."""

# TODO: remove after the esphome dep floor is past the release that ships
# ``esphome.upload_targets``.

from __future__ import annotations

from enum import StrEnum


class PortType(StrEnum):
    SERIAL = "SERIAL"
    NETWORK = "NETWORK"
    MQTT = "MQTT"
    MQTTIP = "MQTTIP"
    BOOTSEL = "BOOTSEL"


def get_port_type(port: str) -> PortType:
    """Classify a user-supplied ``--device`` string."""
    if port == "BOOTSEL":
        return PortType.BOOTSEL
    if port.startswith(("/", "COM")):
        return PortType.SERIAL
    if port == "MQTT":
        return PortType.MQTT
    if port == "MQTTIP":
        return PortType.MQTTIP
    return PortType.NETWORK
