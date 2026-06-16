"""Tests for the generated ``platform_capabilities.index.json`` and its loader.

The dashboard reads ESP32 / LibreTiny / RP2040 platform metadata off this
committed JSON instead of importing ``esphome.components.esp32`` / ``.wifi``
(which drag espidf / requests / esphome.config onto cold start). These pin that
the committed file still matches the installed esphome. The cold-path invariant
(those modules absent after import + start) lives in test_cold_import_floor.py.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from esphome.components.esp32.const import VARIANTS
from esphome.components.libretiny.const import FAMILY_COMPONENT
from esphome.components.rp2040.boards import BOARDS
from esphome.components.wifi import NO_WIFI_VARIANTS

from esphome_device_builder.definitions import load_platform_capabilities_index


def test_loader_returns_known_platforms() -> None:
    """Smoke that the committed index parses into the expected platform data."""
    caps = load_platform_capabilities_index()
    assert "ESP32" in caps.esp32_variants
    assert "ESP32S3" in caps.esp32_variants
    assert set(caps.esp32_no_wifi_variants) == {"ESP32H2", "ESP32P4"}
    assert "bk72xx" in caps.libretiny_families
    # Plain Pico has no native wifi; the Pico W is absent from the no-wifi set.
    assert "rpipico" in caps.rp2040_no_wifi_boards
    assert "rpipicow" not in caps.rp2040_no_wifi_boards


def test_index_matches_installed_esphome() -> None:
    """The committed index equals what script/sync_components.py would emit.

    Catches a committed file that has drifted from the pinned esphome (the
    unit-level mirror of the workflow's regenerate-and-diff gate).
    """
    caps = load_platform_capabilities_index()
    assert caps.esp32_variants == sorted(VARIANTS)
    assert caps.esp32_no_wifi_variants == sorted(NO_WIFI_VARIANTS)
    assert caps.libretiny_families == sorted(set(FAMILY_COMPONENT.values()))
    assert caps.rp2040_no_wifi_boards == sorted(
        board for board, info in BOARDS.items() if not info.get("wifi", False)
    )
    sentinel = SimpleNamespace(name="{name}")
    for component in ("esp32", "esp8266", "rp2040"):
        module = importlib.import_module(f"esphome.components.{component}")
        expected = [
            {
                "title": entry.get("title", ""),
                "description": entry.get("description", ""),
                "file": entry["file"],
            }
            for entry in module.get_download_types(sentinel)
        ]
        assert caps.download_types[component] == expected
