"""Unit tests for ``script/sync_bluetooth_proxies.py`` extraction."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from script.sync_bluetooth_proxies import _iter_configs, _make_record

_OLIMEX_YAML = """\
esphome:
  name: olimex-esp32-poe-iso
  friendly_name: Bluetooth Proxy
  name_add_mac_suffix: true

esp32:
  variant: esp32
  framework:
    type: esp-idf

ethernet:
  type: LAN8720
  mdc_pin: GPIO23
  mdio_pin: GPIO18
  clk:
    pin: GPIO17
    mode: CLK_OUT
  phy_addr: 0
  power_pin:
    number: GPIO12
    ignore_strapping_warning: true

api:
logger:
ota:
  - platform: esphome

esp32_ble:
  max_connections: 4
esp32_ble_tracker:
bluetooth_proxy:
  active: true
"""

_GENERIC_YAML = """\
esphome:
  name: esp32-bluetooth-proxy

esp32:
  variant: esp32
  framework:
    type: esp-idf

wifi:
  ap:

api:
esp32_ble_tracker:
bluetooth_proxy:
  active: true
"""


def _write_repo(root: Path) -> Path:
    (root / "olimex").mkdir(parents=True)
    (root / "olimex" / "olimex-esp32-poe-iso.yaml").write_text(_OLIMEX_YAML, encoding="utf-8")
    (root / "olimex" / "olimex-esp32-poe-iso.factory.yaml").write_text(
        "packages:\n  base: !include olimex-esp32-poe-iso.yaml\n", encoding="utf-8"
    )
    (root / "seeed").mkdir()
    (root / "seeed" / "seeed-esp32-poe.yaml").write_text(
        _GENERIC_YAML.replace("esp32-bluetooth-proxy", "seeed-esp32-poe"), encoding="utf-8"
    )
    # The seeed installer wrapper uses the dash spelling upstream.
    (root / "seeed" / "seeed-esp32-poe-factory.yaml").write_text(
        "packages:\n  base: !include seeed-esp32-poe.yaml\n", encoding="utf-8"
    )
    (root / "esp32-generic").mkdir()
    (root / "esp32-generic" / "esp32-generic.yaml").write_text(_GENERIC_YAML, encoding="utf-8")
    (root / ".github").mkdir()
    return root


def test_iter_configs_skips_factory_wrappers(tmp_path: Path) -> None:
    sources = _iter_configs(_write_repo(tmp_path))
    assert [s.remote_id for s in sources] == [
        "esp32-generic/esp32-generic",
        "olimex/olimex-esp32-poe-iso",
        "seeed/seeed-esp32-poe",
    ]
    assert sources[1].stem == "olimex-esp32-poe-iso"


def test_make_record_olimex_full_shape(tmp_path: Path) -> None:
    """An ethernet proxy record: package fields, mined ethernet, pins, identity row."""
    sources = {s.remote_id: s for s in _iter_configs(_write_repo(tmp_path))}
    record, skip = _make_record(sources["olimex/olimex-esp32-poe-iso"], "abc123")
    assert skip is None
    assert record["id"] == "olimex-esp32-poe-iso-bluetooth-proxy"
    assert record["name"] == "Olimex ESP32-POE-ISO Bluetooth Proxy"
    assert record["esphome"] == {
        "platform": "esp32",
        "board": "esp32-poe-iso",
        "variant": "esp32",
        "framework": "esp-idf",
    }
    assert record["package_name"] == "esphome.bluetooth-proxy"
    assert (
        record["package_import_url"]
        == "github://esphome/bluetooth-proxies/olimex/olimex-esp32-poe-iso.yaml@main"
    )
    assert record["featured"] is True
    assert record["hardware"]["connectivity"] == ["wifi", "bluetooth", "ethernet"]
    # No featured entry: an addable ethernet card would vendor the pinout
    # locally and opt the device out of upstream updates. The connectivity
    # claim plus the pins map carry the wired signal instead.
    assert "featured_components" not in record
    assert {p["gpio"] for p in record["pins"]} == {12, 17, 18, 23}
    assert record["source"] == {
        "type": "bluetooth-proxies",
        "remote_id": "olimex/olimex-esp32-poe-iso",
        "upstream_url": "https://github.com/esphome/bluetooth-proxies/blob/main/olimex/olimex-esp32-poe-iso.yaml",
        "upstream_revision": "abc123",
        "content_hash": record["source"]["content_hash"],
    }


def test_make_record_wifi_generic(tmp_path: Path) -> None:
    """A Wi-Fi proxy record: no ethernet entry / pins, ``is_generic`` from the table."""
    sources = {s.remote_id: s for s in _iter_configs(_write_repo(tmp_path))}
    record, skip = _make_record(sources["esp32-generic/esp32-generic"], "")
    assert skip is None
    assert record["is_generic"] is True
    assert "featured_components" not in record
    assert "pins" not in record
    assert record["hardware"]["connectivity"] == ["wifi", "bluetooth"]
    assert "upstream_revision" not in record["source"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        pytest.param(
            lambda text: text.replace("esp32:", "esp8266:"), "no esp32 block", id="no_esp32"
        ),
        pytest.param(
            lambda text: text.replace("variant: esp32", "variant: esp99"),
            "unknown esp32 variant 'esp99'",
            id="unknown_variant",
        ),
        pytest.param(
            lambda text: text.replace("bluetooth_proxy:", "other_component:"),
            "no bluetooth_proxy block",
            id="not_a_proxy",
        ),
    ],
)
def test_make_record_acceptance_guards(tmp_path: Path, mutation, reason: str) -> None:
    (tmp_path / "olimex").mkdir()
    (tmp_path / "olimex" / "olimex-esp32-poe-iso.yaml").write_text(
        mutation(_OLIMEX_YAML), encoding="utf-8"
    )
    (source,) = _iter_configs(tmp_path)
    record, skip = _make_record(source, "")
    assert record is None
    assert skip == reason


def test_make_record_unknown_device_derives_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A config with no identity row still imports, with a warning and a derived name."""
    (tmp_path / "newvendor").mkdir()
    (tmp_path / "newvendor" / "shiny-proxy-v2.yaml").write_text(_GENERIC_YAML, encoding="utf-8")
    (source,) = _iter_configs(tmp_path)
    with caplog.at_level(logging.WARNING):
        record, skip = _make_record(source, "")
    assert skip is None
    assert record["id"] == "shiny-proxy-v2-bluetooth-proxy"
    assert record["name"] == "Shiny Proxy V2 Bluetooth Proxy"
    assert any("No identity row" in rec.getMessage() for rec in caplog.records)
