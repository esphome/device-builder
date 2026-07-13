"""Pin the full-setup validation gate's error mapping and drop application."""

from __future__ import annotations

from typing import Any

import pytest

import script._full_setup_gate as gate  # type: ignore[import-not-found]
from script._full_setup_gate import (  # type: ignore[import-not-found]
    _apply_drops,
    _map_errors,
    _validate_record,
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
    _apply_drops(record, {"energy"}, {})
    ids = [entry["id"] for entry in record["featured_components"]]
    assert ids == ["power", "relay", "modbus_bus"]
    assert record["featured_components"][1]["requires"] == ["modbus_bus"]
    assert record["featured_bundles"][0]["component_ids"] == ["power", "relay"]


def test_map_errors_tolerates_esphome_tags() -> None:
    """Generated YAML with ``!lambda`` values still parses for path mapping."""
    yaml_text = _YAML.replace(
        "    id: energy\n",
        "    id: energy\n    filters:\n      - lambda: !lambda 'return x;'\n",
    )
    outcome = _map_errors([_Err("bad filter", ["sensor", 0])], yaml_text, _record())
    assert [local_id for local_id, _ in outcome.drops] == ["energy"]


def test_worker_crash_refuses_the_board_not_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected worker exception becomes a board-level refusal."""
    monkeypatch.setattr(
        gate, "_validate_record_inner", lambda record: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    outcome = _validate_record({"id": "board"})
    assert outcome is not None
    assert outcome.drops == []
    assert "boom" in outcome.errors[0]


def test_apply_drops_prunes_dropped_entries_pins() -> None:
    """Pins owned by a dropped entry leave the pins block, by id and by name."""
    record = _record()
    record["featured_components"][0]["fields"]["name"] = "Daily Energy"
    record["pins"] = [
        {"gpio": 4, "available": False, "occupied_by": "energy"},
        {"gpio": 5, "available": False, "occupied_by": "Daily Energy"},
        {"gpio": 6, "available": False, "occupied_by": "relay"},
    ]
    _apply_drops(record, {"energy"}, {})
    assert record["pins"] == [{"gpio": 6, "available": False, "occupied_by": "relay"}]


def test_apply_drops_relabels_shared_pins_to_the_survivor() -> None:
    """A GPIO a surviving entry still locks stays declared under the survivor's label."""
    record = _record()
    record["featured_components"][2]["fields"]["pin"] = {"value": 4, "locked": True}
    record["pins"] = [{"gpio": 4, "available": False, "occupied_by": "energy"}]
    _apply_drops(record, {"energy"}, {})
    assert record["pins"] == [{"gpio": 4, "available": False, "occupied_by": "relay"}]


def test_apply_drops_prunes_component_id_labeled_pins() -> None:
    """A nameless dropped entry's pins carry its component id as the label."""
    record = _record()
    record["pins"] = [{"gpio": 4, "available": False, "occupied_by": "sensor.total_daily_energy"}]
    _apply_drops(record, {"energy"}, {})
    assert "pins" not in record


def test_apply_drops_keeps_bus_pins_via_catalog_typing() -> None:
    """A surviving bus claiming a shared GPIO through ``sda`` keeps the declaration."""
    record = _record()
    record["featured_components"].append(
        {
            "id": "bus",
            "component_id": "i2c",
            "fields": {"sda": {"value": 4, "locked": True}, "id": {"value": "bus"}},
        }
    )
    record["pins"] = [{"gpio": 4, "available": False, "occupied_by": "energy"}]
    components_index = {
        "i2c": {"category": "bus", "config_entries": [{"key": "sda", "type": "pin"}]}
    }
    _apply_drops(record, {"energy"}, components_index)
    assert record["pins"] == [{"gpio": 4, "available": False, "occupied_by": "bus"}]


def test_apply_drops_removes_emptied_bundles() -> None:
    record = _record()
    record["featured_bundles"] = [{"id": "solo", "name": "Solo", "component_ids": ["energy"]}]
    _apply_drops(record, {"energy"}, {})
    assert "featured_bundles" not in record
