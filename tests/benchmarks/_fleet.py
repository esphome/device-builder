"""Synthetic device-fleet builders for the benchmark suite.

Materialises N device YAMLs + StorageJSON sidecars + build_info.json
files on a tmp_path so benchmarks can measure per-device work at
realistic fleet sizes (5, 50, 200). Layout mirrors the HA-addon
``/data`` tree: YAML at ``<config_dir>/<name>.yaml``, sidecar at
``<config_dir>/.esphome/storage/<name>.yaml.json``, build_info at
``<config_dir>/.esphome/build/<name>/build_info.json``. Bytes are
deterministic per index so re-runs are stable.

Storage sidecar + build_info writes go through the shared helpers
in ``tests/_storage_fixtures`` so a schema bump lands in one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from tests._storage_fixtures import write_build_info, write_storage_json

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
    build_root = config_dir / ".esphome" / "build"

    paths: list[Path] = []
    for index in range(n):
        name = f"device_{index:04d}"
        friendly = f"Device {index:04d}"
        configuration = f"{name}.yaml"
        yaml_path = config_dir / configuration
        yaml_path.write_text(
            _YAML_TEMPLATE.format(
                name=name,
                friendly=friendly,
                index=index,
                index_mod_10=index % 10,
                pin=index % 30,
            ),
            encoding="utf-8",
        )
        paths.append(yaml_path)

        build_dir = build_root / name
        # Non-empty ``config_hash`` so the scanner's metadata resolver
        # takes the build_info.json path (production-hot on HA Green),
        # not the sidecar fallback for "build dir wiped".
        write_build_info(build_dir, config_hash=index or 1)

        write_storage_json(
            config_dir,
            configuration,
            firmware_bin_path=build_dir / ".pioenvs" / name / "firmware.bin",
            build_path=build_dir,
            overrides={
                "name": name,
                "friendly_name": friendly,
                "comment": f"Synthetic device {index} for benchmark",
                "address": f"{name}.local",
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
            },
        )

    return sorted(paths)
