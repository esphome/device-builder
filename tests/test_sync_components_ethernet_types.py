"""Pin ethernet's per-platform ``type`` options split."""

from __future__ import annotations

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
_OPTIONS = {
    "esp32": [{"label": t, "value": t} for t in ("DM9051", "LAN8720", "W5500")],
    "rp2040": [{"label": t, "value": t} for t in _RP2_TYPES],
}


def _values(options: list[dict]) -> list[str]:
    return [o["value"] for o in options]


def test_options_derive_from_live_ethernet_module() -> None:
    ethernet = pytest.importorskip("esphome.components.ethernet")
    options = sync_components._ethernet_type_platform_options()
    if not hasattr(ethernet, "RP2_ETHERNET_TYPES"):
        assert options == {}
        return
    assert _values(options["rp2040"]) == sorted(ethernet.RP2_ETHERNET_TYPES)
    esp32 = set(_values(options["esp32"]))
    assert esp32.isdisjoint(set(ethernet.RP2_ETHERNET_TYPES) - set(ethernet.SPI_ETHERNET_TYPES))
    assert {"LAN8720", "W5500", "OPENETH"} <= esp32


def test_apply_stamps_type_and_component(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_components, "_ethernet_type_platform_options", lambda: _OPTIONS)
    component = {
        "id": "ethernet",
        "supported_platforms": [],
        "config_entries": [{"key": "type", "options": []}, {"key": "cs_pin"}],
    }
    sync_components._apply_ethernet_platform_split(component)
    entries = {e["key"]: e for e in component["config_entries"]}
    assert _values(entries["type"]["platform_options"]["rp2040"]) == _RP2_TYPES
    assert "platform_options" not in entries["cs_pin"]
    assert component["supported_platforms"] == ["esp32", "rp2040"]


def test_apply_noop_on_other_components_and_without_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_components, "_ethernet_type_platform_options", lambda: _OPTIONS)
    wifi = {"id": "wifi", "supported_platforms": [], "config_entries": [{"key": "type"}]}
    sync_components._apply_ethernet_platform_split(wifi)
    assert wifi["supported_platforms"] == []
    assert "platform_options" not in wifi["config_entries"][0]

    monkeypatch.setattr(sync_components, "_ethernet_type_platform_options", dict)
    ethernet = {"id": "ethernet", "supported_platforms": [], "config_entries": [{"key": "type"}]}
    sync_components._apply_ethernet_platform_split(ethernet)
    assert ethernet["supported_platforms"] == []
    assert "platform_options" not in ethernet["config_entries"][0]


def test_shipped_ethernet_body_carries_the_split() -> None:
    body = json.loads((_DEFINITIONS / "components" / "ethernet.json").read_text())
    entries = {e["key"]: e for e in body["config_entries"]}
    assert _values(entries["type"]["platform_options"]["rp2040"]) == _RP2_TYPES
    assert set(_values(entries["type"]["platform_options"]["esp32"])).isdisjoint(_RP2_ONLY_TYPES)
    index = json.loads((_DEFINITIONS / "components.index.json").read_text())
    ethernet = next(c for c in index["components"] if c["id"] == "ethernet")
    assert ethernet["supported_platforms"] == ["esp32", "rp2040"]


async def test_resolved_body_scopes_type_options_per_platform(
    session_component_catalog: ComponentCatalog,
) -> None:
    bodies = await session_component_catalog.get_component_bodies(
        component_ids=["ethernet"], platform="rp2040"
    )
    type_entry = next(e for e in bodies["ethernet"].config_entries if e.key == "type")
    assert [o.value for o in type_entry.options] == _RP2_TYPES
    assert type_entry.platform_options is None
