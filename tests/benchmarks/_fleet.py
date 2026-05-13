"""Synthetic device-fleet builder for the benchmark suite."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from tests._storage_fixtures import write_synthetic_device

_YAML_TEMPLATE: Final[str] = """\
esphome:
  name: {name}
  friendly_name: {friendly}
  comment: Synthetic device {index} for benchmark
  area: Bench Room {index_mod_10}

esp32:
  board: esp32-c3-devkitm-1
  framework:
    type: esp-idf

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password

api:
  encryption:
    key: "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg="

logger:
  level: INFO

ota:
  - platform: esphome

binary_sensor:
  - platform: gpio
    pin: GPIO{pin}
    name: "{friendly} Button"
    id: button_{index:04d}
    filters:
      - delayed_on: 50ms

sensor:
  - platform: uptime
    name: "{friendly} Uptime"
    id: uptime_{index:04d}
"""

_STORAGE_LOADED: Final[dict[str, object]] = {
    "loaded_integrations": [
        "api",
        "binary_sensor",
        "logger",
        "ota",
        "sensor",
        "uptime",
        "wifi",
    ],
    "loaded_platforms": ["binary_sensor", "sensor"],
}


def synthesize_fleet(config_dir: Path, n: int) -> list[Path]:
    """Materialise *n* synthetic devices under *config_dir*; return sorted YAML paths."""
    config_dir.mkdir(parents=True, exist_ok=True)
    # Write secrets.yaml so ``!secret`` resolves; otherwise ESPHomeLoader
    # silently falls back to the pure-Python loader (5-10x slower) and the
    # bench stops reflecting production.
    (config_dir / "secrets.yaml").write_text(
        "wifi_ssid: bench-ssid\nwifi_password: bench-password\n",
        encoding="utf-8",
    )
    paths: list[Path] = []
    for index in range(n):
        name = f"device_{index:04d}"
        friendly = f"Device {index:04d}"
        # ``config_hash=index or 1`` keeps the value non-zero so the
        # scanner's metadata resolver takes the build_info.json path
        # (production-hot on HA Green), not the sidecar fallback.
        paths.append(
            write_synthetic_device(
                config_dir,
                name,
                yaml_body=_YAML_TEMPLATE.format(
                    name=name,
                    friendly=friendly,
                    index=index,
                    index_mod_10=index % 10,
                    pin=index % 30,
                ),
                config_hash=index or 1,
                storage_overrides={
                    "name": name,
                    "friendly_name": friendly,
                    "comment": f"Synthetic device {index} for benchmark",
                    "address": f"{name}.local",
                    **_STORAGE_LOADED,
                },
            )
        )
    return sorted(paths)
