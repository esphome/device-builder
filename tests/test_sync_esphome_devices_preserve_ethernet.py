"""``_graft_local_ethernet`` re-grafts a hand-added ethernet block across a re-sync.

Upstream device pages don't carry the ethernet PHY pinout, so the sync
regenerates imported manifests without it. A maintainer hand-adds an
``ethernet`` featured component; these pin that it survives regeneration.
"""

from __future__ import annotations

from typing import Any

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
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
