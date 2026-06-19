"""Pin ethernet preservation across an imported-board re-sync (graft + emit-skip paths)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _emit_manifest,
    _graft_local_ethernet,
)


def _eth_entry(entry_id: str = "onboard_ethernet") -> dict[str, Any]:
    return {
        "id": entry_id,
        "component_id": "ethernet",
        "fields": {"type": {"value": "LAN8720", "locked": True}},
    }


def test_grafts_ethernet_and_connectivity_from_prior() -> None:
    """A prior ethernet entry is prepended and ``ethernet`` connectivity added."""
    record: dict[str, Any] = {
        "hardware": {"connectivity": ["wifi", "bluetooth"]},
        "featured_components": [{"id": "reset", "component_id": "binary_sensor.gpio"}],
    }
    prior = {"featured_components": [_eth_entry()]}

    _graft_local_ethernet(record, prior)

    ids = [fc["id"] for fc in record["featured_components"]]
    assert ids == ["onboard_ethernet", "reset"]  # prepended
    assert record["hardware"]["connectivity"] == ["wifi", "bluetooth", "ethernet"]


def test_noop_when_prior_has_no_ethernet() -> None:
    """Without a prior ethernet entry the record is untouched."""
    record: dict[str, Any] = {
        "hardware": {"connectivity": ["wifi"]},
        "featured_components": [{"id": "relay", "component_id": "switch.gpio"}],
    }
    prior = {"featured_components": [{"id": "relay", "component_id": "switch.gpio"}]}

    _graft_local_ethernet(record, prior)

    assert record["featured_components"] == [{"id": "relay", "component_id": "switch.gpio"}]
    assert record["hardware"]["connectivity"] == ["wifi"]


def test_dedupes_by_id_and_keeps_connectivity_unique() -> None:
    """An id already present in the rebuilt record isn't duplicated."""
    record: dict[str, Any] = {
        "hardware": {"connectivity": ["wifi", "ethernet"]},
        "featured_components": [_eth_entry()],
    }
    prior = {"featured_components": [_eth_entry()]}

    _graft_local_ethernet(record, prior)

    assert [fc["id"] for fc in record["featured_components"]] == ["onboard_ethernet"]
    assert record["hardware"]["connectivity"].count("ethernet") == 1


def test_creates_hardware_block_when_absent() -> None:
    """A record with no ``hardware`` still gains the ethernet connectivity flag."""
    record: dict[str, Any] = {"featured_components": []}
    prior = {"featured_components": [_eth_entry()]}

    _graft_local_ethernet(record, prior)

    assert record["hardware"]["connectivity"] == ["ethernet"]
    assert [fc["id"] for fc in record["featured_components"]] == ["onboard_ethernet"]


def test_preserves_ethernet_occupied_pins_without_clobbering_upstream() -> None:
    """``occupied_by: Ethernet …`` pins are carried over; upstream pins are kept."""
    record: dict[str, Any] = {
        "featured_components": [],
        "pins": [{"gpio": 14, "available": False, "occupied_by": "power_led"}],
    }
    prior = {
        "featured_components": [_eth_entry()],
        "pins": [
            {"gpio": 14, "available": False, "occupied_by": "power_led"},
            {"gpio": 23, "available": False, "occupied_by": "Ethernet MDC"},
            {"gpio": 0, "available": False, "occupied_by": "Ethernet CLK"},
        ],
    }

    _graft_local_ethernet(record, prior)

    gpios = [p["gpio"] for p in record["pins"]]
    assert gpios == [14, 23, 0]  # upstream kept, ethernet appended, no dup of 14


def _write_board(boards_dir: Path, board_id: str, text: str) -> Path:
    target = boards_dir / board_id
    target.mkdir(parents=True)
    manifest = target / "manifest.yaml"
    manifest.write_text(text, encoding="utf-8")
    return manifest


def test_emit_manifest_skips_hand_curated_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing manifest with no ``source.type`` is left untouched (slug collision)."""
    monkeypatch.setattr("script.sync_esphome_devices._BOARDS_DIR", tmp_path)
    original = "id: myboard\nname: Hand Curated\n# no source block\n"
    manifest = _write_board(tmp_path, "myboard", original)

    result = _emit_manifest({"id": "myboard", "name": "Upstream"}, MagicMock())

    assert result is None
    assert manifest.read_text(encoding="utf-8") == original


def test_emit_manifest_preserves_unparsable_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing but unparsable manifest is preserved, not clobbered."""
    monkeypatch.setattr("script.sync_esphome_devices._BOARDS_DIR", tmp_path)
    broken = "{{{ this is not: valid yaml ::::\n"
    manifest = _write_board(tmp_path, "myboard", broken)

    result = _emit_manifest({"id": "myboard", "name": "Upstream"}, MagicMock())

    assert result is None
    assert manifest.read_text(encoding="utf-8") == broken


def test_emit_manifest_overwrites_imported_and_grafts_ethernet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An imported manifest is rewritten, carrying its hand-added ethernet block over."""
    monkeypatch.setattr("script.sync_esphome_devices._BOARDS_DIR", tmp_path)
    prior_text = yaml.safe_dump(
        {
            "id": "myboard",
            "name": "My Board",
            "esphome": {"platform": "esp32", "board": "esp32dev"},
            "hardware": {"connectivity": ["wifi"]},
            "featured_components": [_eth_entry()],
            "source": {"type": "esphome-devices", "remote_id": "MyBoard"},
        },
        sort_keys=False,
    )
    manifest = _write_board(tmp_path, "myboard", prior_text)
    # What the importer rebuilds from upstream — no ethernet, since it
    # can't mine the PHY pinout.
    record = {
        "id": "myboard",
        "name": "My Board",
        "esphome": {"platform": "esp32", "board": "esp32dev"},
        "hardware": {"connectivity": ["wifi", "bluetooth"]},
        "featured_components": [{"id": "relay", "component_id": "switch.gpio"}],
        "source": {"type": "esphome-devices", "remote_id": "MyBoard"},
    }

    result = _emit_manifest(record, MagicMock())

    assert result is not None
    written = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert [fc["id"] for fc in written["featured_components"]] == ["onboard_ethernet", "relay"]
    assert "ethernet" in written["hardware"]["connectivity"]
