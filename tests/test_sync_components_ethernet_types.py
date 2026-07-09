"""Pin ethernet's platform split: per-platform ``type`` options and esp32-only SPI fields."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import esphome_device_builder
from esphome_device_builder.controllers.components import ComponentCatalog

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import sync_components  # noqa: E402

_DEFINITIONS = Path(esphome_device_builder.__file__).parent / "definitions"

_RP2_TYPES = ["ENC28J60", "W5100", "W5500", "W6100", "W6300"]
_RP2_ONLY_TYPES = {"W5100", "W6100", "W6300"}
_ESP32_ONLY_SPI_FIELDS = {
    "clock_speed": ["esp32"],
    "interface": ["esp32"],
    "polling_interval": ["esp32"],
}
_SPLIT = (
    {
        "esp32": [{"label": t, "value": t} for t in ("DM9051", "LAN8720", "W5500")],
        "rp2040": [{"label": t, "value": t} for t in _RP2_TYPES],
    },
    {"clock_speed": ["esp32"]},
)


def _values(options: list[dict]) -> list[str]:
    return [o["value"] for o in options]


def test_split_derives_from_live_ethernet_module() -> None:
    ethernet = pytest.importorskip("esphome.components.ethernet")
    options, field_constraints = sync_components._ethernet_platform_split()
    if not hasattr(ethernet, "RP2_ETHERNET_TYPES"):
        assert (options, field_constraints) == ({}, {})
        return
    assert _values(options["rp2040"]) == sorted(ethernet.RP2_ETHERNET_TYPES)
    esp32 = set(_values(options["esp32"]))
    assert esp32.isdisjoint(set(ethernet.RP2_ETHERNET_TYPES) - set(ethernet.SPI_ETHERNET_TYPES))
    assert {"LAN8720", "W5500", "OPENETH"} <= esp32
    assert field_constraints == _ESP32_ONLY_SPI_FIELDS


def test_apply_stamps_type_fields_and_component(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_components, "_ethernet_platform_split", lambda: _SPLIT)
    component = {
        "id": "ethernet",
        "supported_platforms": [],
        "config_entries": [
            {"key": "type", "options": []},
            {"key": "clock_speed"},
            {"key": "cs_pin"},
        ],
    }
    sync_components._apply_ethernet_platform_split(component)
    entries = {e["key"]: e for e in component["config_entries"]}
    assert _values(entries["type"]["platform_options"]["rp2040"]) == _RP2_TYPES
    assert entries["clock_speed"]["supported_platforms"] == ["esp32"]
    assert "supported_platforms" not in entries["cs_pin"]
    assert component["supported_platforms"] == ["esp32", "rp2040"]


def test_apply_skips_other_components(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_components, "_ethernet_platform_split", lambda: _SPLIT)
    component = {"id": "wifi", "supported_platforms": [], "config_entries": [{"key": "type"}]}
    sync_components._apply_ethernet_platform_split(component)
    assert component["supported_platforms"] == []
    assert "platform_options" not in component["config_entries"][0]


def test_apply_noop_without_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_components, "_ethernet_platform_split", lambda: ({}, {}))
    component = {"id": "ethernet", "supported_platforms": [], "config_entries": [{"key": "type"}]}
    sync_components._apply_ethernet_platform_split(component)
    assert component["supported_platforms"] == []
    assert "platform_options" not in component["config_entries"][0]


def test_shipped_ethernet_body_carries_the_split() -> None:
    body = json.loads((_DEFINITIONS / "components" / "ethernet.json").read_text())
    entries = {e["key"]: e for e in body["config_entries"]}
    assert _values(entries["type"]["platform_options"]["rp2040"]) == _RP2_TYPES
    assert set(_values(entries["type"]["platform_options"]["esp32"])).isdisjoint(_RP2_ONLY_TYPES)
    for key, platforms in _ESP32_ONLY_SPI_FIELDS.items():
        assert entries[key]["supported_platforms"] == platforms
    index = json.loads((_DEFINITIONS / "components.index.json").read_text())
    ethernet = next(c for c in index["components"] if c["id"] == "ethernet")
    assert ethernet["supported_platforms"] == ["esp32", "rp2040"]


async def test_resolved_body_scopes_type_options_per_platform() -> None:
    cat = ComponentCatalog()
    await asyncio.to_thread(cat.load)
    rp2040 = (await cat.get_component_bodies(component_ids=["ethernet"], platform="rp2040"))[
        "ethernet"
    ]
    type_entry = next(e for e in rp2040.config_entries if e.key == "type")
    assert [o.value for o in type_entry.options] == _RP2_TYPES
    assert type_entry.platform_options is None

    esp32 = (await cat.get_component_bodies(component_ids=["ethernet"], platform="esp32"))[
        "ethernet"
    ]
    type_entry = next(e for e in esp32.config_entries if e.key == "type")
    values = {o.value for o in type_entry.options}
    assert "LAN8720" in values
    assert values.isdisjoint(_RP2_ONLY_TYPES)
