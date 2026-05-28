"""Benchmarks for the lazy-body batch hydrate path.

``components/get_component_bodies`` is the navigator's mount-time hot
path: every device on the dashboard fans out one batch request
covering every component its YAML uses, and the backend has to read
each per-id file off disk + build the ``ComponentCatalogEntry``
tree. The benchmark measures the full
``_load_bodies_from_disk`` + ``_load_component`` pipeline against a
curated batch of 100 commonly-used components so a regression in any
layer (orjson decode, dataclass build, nested ``_load_config_entry``
recursion, ``_strip_entry_defaults`` round-trip) is visible.

The id list is hand-picked rather than sliced from the index so the
benchmark stays comparable across catalog re-syncs that reorder or
introduce esoteric components; we don't want a new ``sensor.foo``
landing in the first 100 alphabetical entries to swing the bench.
"""

from __future__ import annotations

from pathlib import Path

from pytest_codspeed import BenchmarkFixture

from esphome_device_builder.controllers.components import _load_bodies_from_disk

_DEFINITIONS = Path(__file__).resolve().parents[2] / "esphome_device_builder" / "definitions"
_BODIES_DIR = _DEFINITIONS / "components"

# 100 commonly-used component ids. Picked to span the catalog's
# breadth: core / network / bus on top, then the platform-domain
# components a typical device pulls in. Every id is verified to
# have a body file on disk; a missing one would silently drop out
# of the batch and shrink the sample, so the smoke assertion below
# the benchmark catches that.
_COMMON_COMPONENT_IDS: list[str] = [
    # Core (10)
    "api",
    "captive_portal",
    "dashboard_import",
    "debug",
    "ethernet",
    "globals",
    "logger",
    "psram",
    "web_server",
    "wifi",
    # Bus / hardware (10)
    "i2c",
    "spi",
    "uart",
    "ble_client",
    "bluetooth_proxy",
    "deep_sleep",
    "external_components",
    "factory_reset",
    "font",
    "graph",
    # Sensors (30)
    "sensor.adc",
    "sensor.aht10",
    "sensor.bh1750",
    "sensor.bme280_i2c",
    "sensor.bme680",
    "sensor.bmp280_i2c",
    "sensor.ccs811",
    "sensor.dallas_temp",
    "sensor.dht",
    "sensor.ads1115",
    "sensor.hdc1080",
    "sensor.htu21d",
    "sensor.ina226",
    "sensor.mhz19",
    "sensor.mpu6050",
    "sensor.pmsx003",
    "sensor.scd30",
    "sensor.scd4x",
    "sensor.sds011",
    "sensor.sgp30",
    "sensor.sgp4x",
    "sensor.sht3xd",
    "sensor.template",
    "sensor.tmp117",
    "sensor.tsl2561",
    "sensor.uptime",
    "sensor.veml7700",
    "sensor.wifi_signal",
    "sensor.tof10120",
    "sensor.pulse_counter",
    # Binary sensors (10)
    "binary_sensor.analog_threshold",
    "binary_sensor.ble_presence",
    "binary_sensor.cap1188",
    "binary_sensor.esp32_touch",
    "binary_sensor.gpio",
    "binary_sensor.homeassistant",
    "binary_sensor.matrix_keypad",
    "binary_sensor.pn532",
    "binary_sensor.status",
    "binary_sensor.template",
    # Switches (10)
    "switch.factory_reset",
    "switch.gpio",
    "switch.homeassistant",
    "switch.output",
    "switch.restart",
    "switch.safe_mode",
    "switch.shutdown",
    "switch.template",
    "switch.uart",
    "switch.copy",
    # Light (10)
    "light.color_temperature",
    "light.binary",
    "light.cwww",
    "light.esp32_rmt_led_strip",
    "light.fastled_clockless",
    "light.monochromatic",
    "light.neopixelbus",
    "light.partition",
    "light.rgb",
    "light.rgbct",
    # Output / button / number / select (10)
    "output.gpio",
    "output.ledc",
    "output.template",
    "button.factory_reset",
    "button.output",
    "button.restart",
    "button.safe_mode",
    "button.shutdown",
    "button.template",
    "number.template",
    # Text sensor / cover / fan / climate (10)
    "text_sensor.template",
    "text_sensor.version",
    "text_sensor.wifi_info",
    "cover.template",
    "cover.endstop",
    "cover.time_based",
    "fan.binary",
    "fan.speed",
    "fan.template",
    "climate.thermostat",
]

# Sanity-check the curated list at module-collection time so a
# stale id is caught up front rather than as a silent CodSpeed
# sample-size drop. Asserting inside the benchmark loop would
# inflate the per-iteration cost.
_MISSING = [cid for cid in _COMMON_COMPONENT_IDS if not (_BODIES_DIR / f"{cid}.json").is_file()]
assert not _MISSING, f"benchmark id list drifted from catalog: {_MISSING}"
assert len(_COMMON_COMPONENT_IDS) == 100


def test_load_100_common_component_bodies(benchmark: BenchmarkFixture) -> None:
    """Pin the batch hydrate cost — one navigator mount loads ~100 bodies.

    Runs the same ``_load_bodies_from_disk`` helper the WS handler
    dispatches into the executor: per-id file read + orjson decode +
    ``_load_component`` dataclass build (which recursively builds
    every ``ConfigEntry`` via ``_load_config_entry``). The single
    executor hop the handler uses is omitted (it's a constant
    overhead the bench doesn't need to re-measure); this is the
    portion of the request that scales with batch size.
    """
    # Smoke-validate ONCE outside the loop so a refactor that turned
    # ``_load_body_from_disk`` into a no-op surfaces here rather
    # than as a fast-but-empty CodSpeed "speedup".
    smoke = _load_bodies_from_disk(_COMMON_COMPONENT_IDS)
    assert len(smoke) == 100
    assert all(entry is not None for entry in smoke.values())

    @benchmark
    def run() -> None:
        _load_bodies_from_disk(_COMMON_COMPONENT_IDS)
