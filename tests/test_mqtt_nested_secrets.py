"""MQTT broker secrets for devices below ``devices/`` come from the secrets file beside them."""

from __future__ import annotations

from pathlib import Path

from esphome_device_builder.models import Device
from tests._mqtt_fixtures import RecordingMonitor, build_test_extract, make_mqtt_coordinator

_MQTT = "mqtt:\n  broker: !secret {key}\n  password: !secret mqtt_pw\n"


def _nested_mqtt_device(config_dir: Path, configuration: str, key: str) -> Device:
    path = config_dir / configuration
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = f"esphome:\n  name: {path.stem}\n\n{_MQTT.format(key=key)}"
    path.write_text(yaml, encoding="utf-8")
    return Device(
        name=path.stem,
        friendly_name=path.stem,
        configuration=configuration,
        uses_mqtt=True,
        mqtt_extract=build_test_extract(path, yaml),
    )


async def test_nested_devices_resolve_secrets_from_their_own_folder(
    tmp_path: Path, stub_monitor: type[RecordingMonitor]
) -> None:
    (tmp_path / "secrets.yaml").write_text("mqtt_a: 10.0.0.1\nmqtt_pw: top\n")
    (tmp_path / "devices" / "lights").mkdir(parents=True)
    (tmp_path / "devices" / "lights" / "secrets.yaml").write_text(
        "mqtt_a: 10.0.0.2\nmqtt_b: 10.0.0.3\nmqtt_pw: lights\n"
    )
    devices = [
        _nested_mqtt_device(tmp_path, "top.yaml", "mqtt_a"),
        _nested_mqtt_device(tmp_path, "devices/lights/desk.yaml", "mqtt_a"),
        _nested_mqtt_device(tmp_path, "devices/lights/bar.yaml", "mqtt_b"),
    ]
    coord = make_mqtt_coordinator(tmp_path, devices)

    await coord.reconcile()

    brokers = {(m.broker.host, m.broker.password) for m in stub_monitor.instances}
    assert brokers == {("10.0.0.1", "top"), ("10.0.0.2", "lights"), ("10.0.0.3", "lights")}
    await coord.stop()
