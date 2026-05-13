"""Synthetic device-fleet builders for the benchmark suite.

Materialises N device YAMLs + StorageJSON sidecars + build_info.json
files on a tmp_path so benchmarks can measure per-device work at
realistic fleet sizes (5, 50, 200). Layout mirrors the HA-addon
``/data`` tree: YAML at ``<config_dir>/<name>.yaml``, sidecar at
``<config_dir>/.esphome/storage/<name>.yaml.json``, build_info at
``<config_dir>/.esphome/build/<name>/build_info.json``. Bytes are
deterministic per index so re-runs are stable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

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


def synthesize_fleet(config_dir: Path, n: int) -> list[Path]:
    """Materialise *n* synthetic devices under *config_dir*; return sorted YAML paths."""
    config_dir.mkdir(parents=True, exist_ok=True)
    storage_dir = config_dir / ".esphome" / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    build_root = config_dir / ".esphome" / "build"
    build_root.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    for index in range(n):
        name = f"device_{index:04d}"
        friendly = f"Device {index:04d}"
        yaml_body = _YAML_TEMPLATE.format(
            name=name,
            friendly=friendly,
            index=index,
            index_mod_10=index % 10,
            pin=index % 30,
        )
        yaml_path = config_dir / f"{name}.yaml"
        yaml_path.write_text(yaml_body, encoding="utf-8")
        paths.append(yaml_path)

        build_dir = build_root / name
        build_dir.mkdir(parents=True, exist_ok=True)
        # Non-empty ``config_hash`` so the scanner's metadata resolver
        # takes the build_info.json path (the production-hot codepath
        # on HA Green), not the sidecar fallback for "build dir wiped".
        (build_dir / "build_info.json").write_text(
            json.dumps(
                {
                    "config_hash": f"{index:08x}",
                    "esphome_version": "2026.5.0",
                    "src_version": 1,
                }
            ),
            encoding="utf-8",
        )

        (storage_dir / f"{name}.yaml.json").write_text(
            json.dumps(
                {
                    "storage_version": 1,
                    "name": name,
                    "friendly_name": friendly,
                    "comment": f"Synthetic device {index} for benchmark",
                    "esphome_version": "2026.5.0",
                    "src_version": 1,
                    "address": f"{name}.local",
                    "web_port": None,
                    "esp_platform": "esp32",
                    "board": "esp32-c3-devkitm-1",
                    "build_path": str(build_dir),
                    "firmware_bin_path": str(build_dir / ".pioenvs" / name / "firmware.bin"),
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
                    "no_mdns": False,
                    "framework": "esp-idf",
                    "core_platform": "esp32",
                    "target_platform": "esp32",
                }
            ),
            encoding="utf-8",
        )

    return sorted(paths)
