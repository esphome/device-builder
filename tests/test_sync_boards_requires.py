"""
Pin the sync-time ``requires`` inference for featured components.

A featured field whose value equals a sibling's emitted id is a prerequisite;
the stamp fills ``requires`` (transitively) so the frontend adds the sibling
first. The committed-catalog test guards every manifest against the same
undefined-id-reference bug that shipped for apollo-esk-1's rtttl player.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import orjson

import script.sync_boards as sb
from esphome_device_builder.models.boards import FeaturedComponent
from esphome_device_builder.models.common import FieldPreset


def _fc(
    local_id: str, fields: dict[str, Any], requires: list[str] | None = None
) -> FeaturedComponent:
    return FeaturedComponent(
        id=local_id,
        component_id=f"x.{local_id}",
        fields={k: FieldPreset(value=v) for k, v in fields.items()},
        requires=list(requires or []),
    )


def _stamp(components: list[FeaturedComponent]) -> dict[str, list[str]]:
    sb._stamp_featured_requires([SimpleNamespace(featured_components=components)])
    return {fc.id: fc.requires for fc in components}


def test_infers_requires_from_sibling_reference() -> None:
    out = _fc("buzzer_output", {"id": "buzzer_output", "pin": 18})
    rtttl = _fc("rtttl_player", {"id": "rtttl_player", "output": "buzzer_output"})
    result = _stamp([out, rtttl])
    assert result == {"buzzer_output": [], "rtttl_player": ["buzzer_output"]}


def test_preserves_and_unions_hand_authored_requires() -> None:
    bus = _fc("i2c_bus", {"id": "i2c_bus"})
    hub = _fc("expander", {"id": "expander"})
    sensor = _fc("sensor", {"id": "sensor", "i2c_id": "i2c_bus"}, requires=["expander"])
    result = _stamp([bus, hub, sensor])
    # Hand-authored ``expander`` kept and ordered before the inferred bus.
    assert result["sensor"] == ["expander", "i2c_bus"]


def test_skips_self_reference() -> None:
    # A field pointing at the component's own emitted id must not self-require.
    fc = _fc("thing", {"id": "thing", "self_ref": "thing"})
    assert _stamp([fc]) == {"thing": []}


def test_no_requires_when_value_matches_no_sibling() -> None:
    fc = _fc("thing", {"id": "thing", "name": "Some Name", "pin": 4})
    assert _stamp([fc]) == {"thing": []}


def test_flattens_transitive_chain() -> None:
    a = _fc("a", {"id": "a", "ref": "b"})
    b = _fc("b", {"id": "b", "ref": "c"})
    c = _fc("c", {"id": "c"})
    result = _stamp([a, b, c])
    # ``c`` (deepest) is ordered before ``b`` so each dep lands after its own.
    assert result["a"] == ["c", "b"]
    assert result["b"] == ["c"]
    assert result["c"] == []


def test_cycle_is_safe() -> None:
    a = _fc("a", {"id": "a", "ref": "b"})
    b = _fc("b", {"id": "b", "ref": "a"})
    result = _stamp([a, b])
    assert result["a"] == ["b"]
    assert result["b"] == ["a"]


def test_ignores_non_string_field_values() -> None:
    bus = _fc("bus", {"id": "bus"})
    # dict / list / int values never resolve to a sibling id.
    dev = _fc("dev", {"id": "dev", "nested": {"value": "bus"}, "pins": ["bus"], "n": 3})
    assert _stamp([bus, dev]) == {"bus": [], "dev": []}


# ---------------------------------------------------------------------------
# Committed-catalog invariants (no ESPHome import needed).
# ---------------------------------------------------------------------------


def _load_featured_index() -> dict[str, list[dict[str, Any]]]:
    return orjson.loads(sb._FEATURED_INDEX_FILE.read_bytes())


def _preset_value(preset: Any) -> Any:
    return preset.get("value") if isinstance(preset, dict) else preset


def test_committed_catalog_declares_every_sibling_reference() -> None:
    """Every featured field pointing at a sibling's emitted id is in ``requires``."""
    index = _load_featured_index()
    missing: list[str] = []
    for board_id, comps in index.items():
        emitted = {
            _preset_value(c["fields"]["id"]): c["id"]
            for c in comps
            if isinstance(_preset_value(c.get("fields", {}).get("id")), str)
        }
        for c in comps:
            requires = set(c.get("requires", []))
            for key, preset in c.get("fields", {}).items():
                if key == "id":
                    continue
                value = _preset_value(preset)
                target = emitted.get(value) if isinstance(value, str) else None
                if target is not None and target != c["id"] and target not in requires:
                    missing.append(f"{board_id}/{c['id']} references {value!r}")
    assert not missing, (
        "featured components reference a sibling without requiring it: " + "; ".join(missing)
    )


def test_apollo_esk_1_rtttl_and_battery_requires() -> None:
    body = orjson.loads((sb._BODIES_DIR / "apollo-esk-1.json").read_bytes())
    by_id = {fc["id"]: fc for fc in body["featured_components"]}
    assert by_id["rtttl_player"]["requires"] == ["buzzer_output"]
    assert by_id["battery_monitor"]["requires"] == ["i2c_bus"]
