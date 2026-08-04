"""Shared MQTT-coordinator test fixtures (recording monitor, device factory)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from esphome_device_builder.controllers._device_mqtt_coordinator import DeviceMqttCoordinator
from esphome_device_builder.controllers._device_mqtt_monitor import MqttBrokerConfig
from esphome_device_builder.helpers.device_yaml import extract_mqtt_block
from esphome_device_builder.helpers.subscriber_presence import SubscriberPresence
from esphome_device_builder.models import Device
from esphome_device_builder.models.devices import DeviceMqttExtract


class RecordingMonitor:
    """Stand-in for ``DeviceMqttMonitor`` that records lifecycle calls."""

    instances: ClassVar[list[RecordingMonitor]] = []

    def __init__(self, broker: MqttBrokerConfig, *_args: object, **_kwargs: object) -> None:
        self.broker = broker
        self.presence = _kwargs.get("presence")
        self.on_connection_change = _kwargs.get("on_connection_change")
        self.is_publisher = True
        self.connected = False
        self.started = False
        self.stopped = False
        self.__class__.instances.append(self)

    def set_publisher(self, *, value: bool) -> None:
        self.is_publisher = value

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


def build_test_extract(
    path: Path,
    yaml_content: str,
    resolved_config: dict | None = None,
    resolved_substitutions: dict[str, str] | None = None,
) -> DeviceMqttExtract:
    """Assemble a ``DeviceMqttExtract`` for *path* from *yaml_content* and *resolved_config*."""
    main_block, main_subs = extract_mqtt_block(yaml_content)
    resolved_block = resolved_config.get("mqtt") if isinstance(resolved_config, dict) else None
    secrets = path.parent / "secrets.yaml"
    stat = path.stat()
    return DeviceMqttExtract(
        yaml_mtime=stat.st_mtime,
        yaml_size=stat.st_size,
        secrets_mtime=secrets.stat().st_mtime if secrets.exists() else 0.0,
        main_block=main_block,
        main_substitutions=main_subs,
        resolved_block=resolved_block if isinstance(resolved_block, dict) else None,
        resolved_substitutions=resolved_substitutions or {},
    )


def write_mqtt_device(config_dir: Path, name: str, mqtt_yaml: str | None) -> Device:
    """Write a device YAML and build its Device with the scan-time mqtt extract."""
    yaml = f"esphome:\n  name: {name}\n"
    if mqtt_yaml is not None:
        yaml += f"\n{mqtt_yaml}"
    path = config_dir / f"{name}.yaml"
    path.write_text(yaml)
    return Device(
        name=name,
        friendly_name=name,
        configuration=f"{name}.yaml",
        uses_mqtt=mqtt_yaml is not None,
        mqtt_extract=build_test_extract(path, yaml) if mqtt_yaml is not None else None,
    )


def make_mqtt_coordinator(
    config_dir: Path,
    devices: list[Device],
    presence: SubscriberPresence | None = None,
) -> DeviceMqttCoordinator:
    return DeviceMqttCoordinator(
        config_dir=config_dir,
        get_devices=lambda: devices,
        on_state_change=lambda *_args: None,
        on_ip_change=lambda *_args: None,
        presence=presence,
    )
