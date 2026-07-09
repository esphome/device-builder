"""Tests for the generated ``platform_capabilities.index.json`` and its loader.

The dashboard reads ESP32 / LibreTiny / RP2040 platform metadata off this
committed JSON instead of importing ``esphome.components.esp32`` / ``.wifi``
(which drag espidf / requests / esphome.config onto cold start). These pin that
the committed file parses correctly and stays within the installed esphome's
platform data. The cold-path invariant (those modules absent after import +
start) lives in test_cold_import_floor.py.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import orjson
from esphome.components.esp32.const import VARIANTS
from esphome.components.libretiny.const import FAMILY_COMPONENT
from esphome.components.rp2040.boards import BOARDS
from esphome.components.wifi import NO_WIFI_VARIANTS

from esphome_device_builder.definitions import (
    PlatformCapabilities,
    _load_platform_capabilities,
    _parse_download_types,
    load_platform_capabilities_index,
)

from .conftest import catalog_releases_ahead as _catalog_releases_ahead

_EMPTY = PlatformCapabilities([], [], [], [], {}, [])


def test_loader_returns_known_platforms() -> None:
    """Smoke that the committed index parses into the expected platform data."""
    caps = load_platform_capabilities_index()
    assert "ESP32" in caps.esp32_variants
    assert "ESP32S3" in caps.esp32_variants
    # Subset, not equality — upstream grows the no-wifi set (2026.7 added
    # ESP32H21 / ESP32H4); pin the stable members plus wifi-capable
    # exclusions so a new variant doesn't churn this smoke test.
    assert {"ESP32H2", "ESP32P4"} <= set(caps.esp32_no_wifi_variants)
    assert "ESP32" not in caps.esp32_no_wifi_variants
    assert "ESP32S3" not in caps.esp32_no_wifi_variants
    assert "bk72xx" in caps.libretiny_families
    # Plain Pico has no native wifi; the Pico W is absent from the no-wifi set.
    assert "rpipico" in caps.rp2040_no_wifi_boards
    assert "rpipicow" not in caps.rp2040_no_wifi_boards


def test_index_within_installed_esphome() -> None:
    """
    Committed index stays within one esphome release of the installed one.

    Tolerate extras on whichever side leads (runtime ahead in the beta/dev
    matrix; catalog ahead when it ships before the docker image's esphome),
    capping a catalog lead at one release. Exact parity is the sync workflow's
    regenerate-and-diff gate.
    """
    caps = load_platform_capabilities_index()
    ahead = _catalog_releases_ahead()
    assert ahead <= 1, (
        f"committed catalog is {ahead} esphome releases ahead of the installed esphome; "
        "the runtime may trail the catalog by at most one release"
    )
    installed_no_wifi_boards = {
        board for board, info in BOARDS.items() if not info.get("wifi", False)
    }
    sentinel = SimpleNamespace(name="{name}")
    pairs = [
        (set(caps.esp32_variants), set(VARIANTS)),
        (set(caps.esp32_no_wifi_variants), set(NO_WIFI_VARIANTS)),
        (set(caps.libretiny_families), set(FAMILY_COMPONENT.values())),
        (set(caps.rp2040_no_wifi_boards), installed_no_wifi_boards),
    ]
    for component in ("esp32", "esp8266", "rp2040"):
        module = importlib.import_module(f"esphome.components.{component}")
        upstream = {entry["file"] for entry in module.get_download_types(sentinel)}
        pairs.append(({entry["file"] for entry in caps.download_types[component]}, upstream))

    # Within one release the newer side is a superset of the older, so assert a
    # subset in the direction set by which side leads: committed index ⊆
    # installed when the runtime leads or matches, installed ⊆ committed index
    # when the catalog leads (it ships before the docker image's esphome).
    for indexed, installed in pairs:
        if ahead >= 1:
            missing = installed - indexed
            assert not missing, f"installed esphome data missing from the newer catalog: {missing}"
        else:
            extra = indexed - installed
            assert not extra, f"committed index data no installed esphome exposes: {extra}"


def test_load_missing_index_is_empty(tmp_path: Path) -> None:
    """A missing index degrades to empty (fail-open), not a raise."""
    assert _load_platform_capabilities(tmp_path / "absent.json") == _EMPTY


def test_load_malformed_index_is_empty(tmp_path: Path) -> None:
    """Unparsable JSON degrades to empty."""
    path = tmp_path / "bad.json"
    path.write_bytes(b"{not valid json")
    assert _load_platform_capabilities(path) == _EMPTY


def test_load_non_mapping_index_is_empty(tmp_path: Path) -> None:
    """A top-level JSON array (not an object) degrades to empty."""
    path = tmp_path / "list.json"
    path.write_bytes(b"[]")
    assert _load_platform_capabilities(path) == _EMPTY


def test_load_coerces_non_list_fields(tmp_path: Path) -> None:
    """A field that isn't a list of strings drops to ``[]``; good fields survive."""
    path = tmp_path / "x.json"
    path.write_bytes(
        orjson.dumps({"esp32_variants": "notalist", "libretiny_families": ["bk72xx", 7]})
    )
    caps = _load_platform_capabilities(path)
    assert caps.esp32_variants == []
    assert caps.libretiny_families == ["bk72xx"]  # the non-str 7 is filtered


def test_parse_download_types_drops_malformed() -> None:
    """Non-list components, non-dict entries, and entries without a str file are dropped."""
    parsed = _parse_download_types(
        {
            "esp32": [
                {"title": "A", "description": "d", "file": "f.bin"},
                {"title": "no file"},
                "not a dict",
            ],
            "esp8266": [{"file": "g.bin"}],
            "bad": "not a list",
        }
    )
    assert parsed["esp32"] == [{"title": "A", "description": "d", "file": "f.bin"}]
    assert parsed["esp8266"] == [{"title": "", "description": "", "file": "g.bin"}]
    assert "bad" not in parsed


def test_parse_download_types_non_dict_is_empty() -> None:
    """A non-dict ``download_types`` block yields an empty map."""
    assert _parse_download_types([]) == {}
