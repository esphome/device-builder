"""Real-``esphome config`` pin for ``/json-config`` resolution (#1765).

Spawns a real ``esphome config --show-secrets`` to prove the end-to-end
contract HA depends on: substitutions expanded, packages merged, secrets
resolved, floats/lambdas serialisable — none of which a raw ``load_yaml``
produces.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from esphome_device_builder.helpers.device_yaml import run_esphome_config
from esphome_device_builder.helpers.json import dumps_str


async def test_run_esphome_config_resolves_substitutions_packages_and_secrets(
    tmp_path: Path,
) -> None:
    """Resolved JSON carries the real key (package + secret), expanded name, float."""
    key = base64.b64encode(os.urandom(32)).decode()
    (tmp_path / "secrets.yaml").write_text(
        f'api_key: "{key}"\nwifi_pw: "wifipass8"\n', encoding="utf-8"
    )
    (tmp_path / "base_pkg.yaml").write_text(
        "api:\n  encryption:\n    key: !secret api_key\n", encoding="utf-8"
    )
    (tmp_path / "test.yaml").write_text(
        "substitutions:\n  devname: livingroom\n"
        "packages:\n  base: !include base_pkg.yaml\n"
        "esphome:\n  name: ${devname}\n"
        "esp32:\n  variant: esp32c3\n  framework:\n    type: esp-idf\n"
        "wifi:\n  ssid: myssid\n  password: !secret wifi_pw\n"
        "sensor:\n  - platform: adc\n    pin: GPIO01\n    id: test_adc\n"
        "    filters:\n      - delta: 0.1\n",
        encoding="utf-8",
    )

    config = await run_esphome_config([sys.executable, "-m", "esphome"], tmp_path / "test.yaml")

    assert config is not None, "esphome config should validate and resolve the repro"
    assert config["esphome"]["name"] == "livingroom"  # substitution expanded
    assert config["api"]["encryption"]["key"] == key  # package merged + secret resolved
    assert config["sensor"][0]["filters"][0]["delta"] == 0.1  # float, not a 500
    assert dumps_str(config)  # serialises with plain orjson
