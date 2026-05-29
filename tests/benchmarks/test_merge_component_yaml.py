"""``merge_component_yaml`` benchmarks across 100 / 500 / 1000-line existing YAMLs."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.helpers.json import loads
from esphome_device_builder.helpers.yaml import merge_component_yaml
from esphome_device_builder.models import ComponentCatalogEntry

_DEFINITIONS = Path(__file__).resolve().parents[2] / "esphome_device_builder" / "definitions"

# Pre-extract a real catalog entry at module-collection time, same
# pattern as ``test_startup.py``. ``sensor.dht`` is a platform-style
# component with nested config entries so the bench exercises both
# the recursive generate and the splice-under-domain path.
_SENSOR_DHT = ComponentCatalogEntry.from_dict(
    loads((_DEFINITIONS / "components" / "sensor.dht.json").read_bytes())
)

_FIELDS = {
    "name": "Living Room DHT",
    "id": "living_room_dht",
    "pin": "GPIO4",
    "model": "DHT22",
}


def _generate_yaml(target_lines: int) -> str:
    """Build a synthetic YAML with an existing ``sensor:`` block to splice into."""
    parts = [
        "esphome:",
        "  name: bench_device",
        "  friendly_name: Bench Device",
        "",
        "esp32:",
        "  board: esp32-c3-devkitm-1",
        "  framework:",
        "    type: esp-idf",
        "",
        "wifi:",
        "  ssid: !secret wifi_ssid",
        "  password: !secret wifi_password",
        "",
        "api:",
        "logger:",
        "ota:",
        "  - platform: esphome",
        "",
        "sensor:",
        "  - platform: uptime",
        "    name: Uptime",
        "    id: bench_uptime",
        "",
        "binary_sensor:",
    ]
    needed = max(0, (target_lines - len(parts)) // 6)
    for i in range(needed):
        parts.append("  - platform: gpio")
        parts.append(f"    pin: GPIO{i % 30}")
        parts.append(f'    name: "Bench Sensor {i:04d}"')
        parts.append(f"    id: sensor_{i:04d}")
        parts.append("    filters:")
        parts.append("      - delayed_on: 50ms")
    return "\n".join(parts) + "\n"


# 100 / 500 / 1000 brackets a freshly-stubbed YAML, a typical
# config with a handful of components, and a packaged config.
_YAML_100 = _generate_yaml(100)
_YAML_500 = _generate_yaml(500)
_YAML_1000 = _generate_yaml(1000)


@pytest.mark.parametrize(
    "existing_yaml",
    [
        pytest.param(_YAML_100, id="100"),
        pytest.param(_YAML_500, id="500"),
        pytest.param(_YAML_1000, id="1000"),
    ],
)
def test_merge_component_yaml_sizes(
    benchmark: BenchmarkFixture,
    existing_yaml: str,
) -> None:
    """Splice-into-existing-block cost scales with existing-config line count."""
    warm = merge_component_yaml(existing_yaml, _SENSOR_DHT, _FIELDS)
    assert "Living Room DHT" in warm
    assert "living_room_dht" in warm
    # Spliced under the existing ``sensor:`` block, not duplicated or appended.
    assert warm.count("\nsensor:\n") == 1
    assert warm.index("platform: dht") < warm.index("binary_sensor:")

    @benchmark
    def run() -> None:
        merge_component_yaml(existing_yaml, _SENSOR_DHT, _FIELDS)
