"""Pin the full-setup validation gate's error mapping and drop application."""

from __future__ import annotations

from typing import Any

from script._full_setup_gate import (  # type: ignore[import-not-found]
    _apply_drops,
    _map_errors,
)


class _Err:
    """Stand-in for ``vol.Invalid``: a structured path plus a message."""

    def __init__(self, message: str, path: list[Any] | None = None) -> None:
        self._message = message
        self.path = path or []

    def __str__(self) -> str:
        return self._message


_YAML = """
esphome:
  name: repro
sensor:
  - platform: total_daily_energy
    id: energy
  - platform: hlw8012
    id: power
switch:
  - platform: gpio
    id: relay
modbus:
  id: modbus_bus
"""


def _record() -> dict[str, Any]:
    return {
        "id": "board",
        "featured_components": [
            {"id": "energy", "component_id": "sensor.total_daily_energy", "fields": {}},
            {"id": "power", "component_id": "sensor.hlw8012", "fields": {}},
            {"id": "relay", "component_id": "switch.gpio", "fields": {}},
            {"id": "modbus_bus", "component_id": "modbus", "fields": {}},
        ],
        "featured_bundles": [
            {"id": "all", "name": "All", "component_ids": ["energy", "power", "relay"]},
        ],
    }


def test_maps_indexed_path_to_item_id() -> None:
    """A ``['sensor', 0]`` path resolves through the generated item's ``id``."""
    outcome = _map_errors(
        [_Err("requires component time", ["sensor", 0])],
        _YAML,
        _record(),
    )
    assert [local_id for local_id, _ in outcome.drops] == ["energy"]
    assert outcome.errors == []


def test_maps_mapping_path_to_sole_domain_entry() -> None:
    """A top-level mapping path (``['modbus']``) falls back to the domain's sole entry."""
    outcome = _map_errors([_Err("requires component uart", ["modbus"])], _YAML, _record())
    assert [local_id for local_id, _ in outcome.drops] == ["modbus_bus"]


def test_two_errors_one_entry_dedupes() -> None:
    outcome = _map_errors(
        [
            _Err("bad pin", ["switch", 0, "pin"]),
            _Err("bad mode", ["switch", 0, "pin", "mode"]),
        ],
        _YAML,
        _record(),
    )
    assert [local_id for local_id, _ in outcome.drops] == ["relay"]


def test_unmappable_error_poisons_the_board() -> None:
    """An error with no config path (or an unknown target) maps to a board-level failure."""
    outcome = _map_errors([_Err("something exploded, no path")], _YAML, _record())
    assert outcome.drops == []
    assert outcome.errors == ["something exploded, no path"]


def test_ambiguous_domain_fallback_poisons_the_board() -> None:
    """A path whose item id is unknown and whose domain has several entries can't map."""
    yaml_text = _YAML.replace("id: power", "id: not_featured")
    outcome = _map_errors([_Err("boom", ["sensor", 1])], yaml_text, _record())
    assert outcome.drops == []
    assert outcome.errors == ["boom"]


def test_apply_drops_prunes_entries_bundles_and_requires() -> None:
    record = _record()
    record["featured_components"][2]["requires"] = ["modbus_bus", "energy"]
    _apply_drops(record, {"energy"})
    ids = [entry["id"] for entry in record["featured_components"]]
    assert ids == ["power", "relay", "modbus_bus"]
    assert record["featured_components"][1]["requires"] == ["modbus_bus"]
    assert record["featured_bundles"][0]["component_ids"] == ["power", "relay"]


def test_apply_drops_removes_emptied_bundles() -> None:
    record = _record()
    record["featured_bundles"] = [{"id": "solo", "name": "Solo", "component_ids": ["energy"]}]
    _apply_drops(record, {"energy"})
    assert "featured_bundles" not in record
