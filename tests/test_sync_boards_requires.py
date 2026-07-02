"""
Pin the sync-time ``requires`` inference for featured components.

A featured *reference* field pointing at a sibling's emitted id becomes a
prerequisite, flattened transitively. The committed-catalog test asserts every
such reference in the shipped index is declared in that component's ``requires``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import orjson
import pytest

import script.sync_boards as sb
from esphome_device_builder.models.boards import FeaturedComponent
from esphome_device_builder.models.common import FieldPreset


def _fc(
    local_id: str, fields: dict[str, Any], requires: list[str] | None = None
) -> FeaturedComponent:
    return FeaturedComponent(
        id=local_id,
        component_id=local_id,
        fields={k: FieldPreset(value=v) for k, v in fields.items()},
        requires=list(requires or []),
    )


def _stamp(
    components: list[FeaturedComponent],
    reference_keys: dict[str, frozenset[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, list[str]]:
    """Run the stamp with the catalog reference-key lookup stubbed per component_id."""
    monkeypatch.setattr(
        sb, "_component_reference_keys", lambda cid: reference_keys.get(cid, frozenset())
    )
    sb._stamp_featured_requires([SimpleNamespace(featured_components=components)])
    return {fc.id: fc.requires for fc in components}


# ---------------------------------------------------------------------------
# Reference inference — which fields become requires.
# ---------------------------------------------------------------------------


def test_infers_requires_from_reference_field(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _fc("buzzer_output", {"id": "buzzer_output", "pin": 18})
    rtttl = _fc("rtttl_player", {"id": "rtttl_player", "output": "buzzer_output"})
    result = _stamp([out, rtttl], {"rtttl_player": frozenset({"output"})}, monkeypatch)
    assert result == {"buzzer_output": [], "rtttl_player": ["buzzer_output"]}


def test_ignores_non_reference_field_matching_an_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # A free-text field (``name``) whose value happens to equal a sibling id is
    # not a cross-reference, so it must not infer a dependency.
    bus = _fc("i2c_bus", {"id": "i2c_bus"})
    dev = _fc("dev", {"id": "dev", "name": "i2c_bus"})
    result = _stamp([bus, dev], {"dev": frozenset()}, monkeypatch)
    assert result == {"i2c_bus": [], "dev": []}


def test_preserves_and_unions_hand_authored_requires(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = _fc("i2c_bus", {"id": "i2c_bus"})
    hub = _fc("expander", {"id": "expander"})
    sensor = _fc("sensor", {"id": "sensor", "i2c_id": "i2c_bus"}, requires=["expander"])
    result = _stamp([bus, hub, sensor], {"sensor": frozenset({"i2c_id"})}, monkeypatch)
    # Hand-authored ``expander`` kept and ordered before the inferred bus.
    assert result["sensor"] == ["expander", "i2c_bus"]


def test_skips_self_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reference field pointing at the component's own emitted id must not self-require.
    fc = _fc("thing", {"id": "thing", "self_ref": "thing"})
    assert _stamp([fc], {"thing": frozenset({"self_ref"})}, monkeypatch) == {"thing": []}


def test_ignores_non_string_reference_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reference key whose preset value is a dict/list never resolves to a sibling id.
    bus = _fc("bus", {"id": "bus"})
    dev = _fc("dev", {"id": "dev", "ref": {"value": "bus"}})
    assert _stamp([bus, dev], {"dev": frozenset({"ref"})}, monkeypatch) == {"bus": [], "dev": []}


def test_component_reference_keys_reads_catalog_metadata() -> None:
    """``_component_reference_keys`` picks up the catalog's cross-reference fields."""
    keys = sb._component_reference_keys("rtttl")
    assert "output" in keys
    assert "id" not in keys


# ---------------------------------------------------------------------------
# Transitive closure — pure graph ordering, independent of the catalog.
# ---------------------------------------------------------------------------


def test_flatten_orders_each_dep_after_its_own_deps() -> None:
    direct = {"a": ["b"], "b": ["c"], "c": []}
    # ``c`` (deepest) is ordered before ``b`` so each dep lands after its own.
    assert sb._flatten_requires("a", direct) == ["c", "b"]
    assert sb._flatten_requires("b", direct) == ["c"]
    assert sb._flatten_requires("c", direct) == []


def test_flatten_is_cycle_safe() -> None:
    direct = {"a": ["b"], "b": ["a"]}
    assert sb._flatten_requires("a", direct) == ["b"]
    assert sb._flatten_requires("b", direct) == ["a"]


# ---------------------------------------------------------------------------
# Committed-catalog invariant (no ESPHome import needed).
# ---------------------------------------------------------------------------


def _load_featured_index() -> dict[str, list[dict[str, Any]]]:
    return orjson.loads(sb._FEATURED_INDEX_FILE.read_bytes())


def _preset_value(preset: Any) -> Any:
    return preset.get("value") if isinstance(preset, dict) else preset


def test_committed_catalog_declares_every_reference() -> None:
    """Every featured reference field pointing at a sibling's emitted id is in ``requires``."""
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
            for key in sb._component_reference_keys(c["component_id"]):
                value = _preset_value(c.get("fields", {}).get(key))
                target = emitted.get(value) if isinstance(value, str) else None
                if target is not None and target != c["id"] and target not in requires:
                    missing.append(f"{board_id}/{c['id']} references {value!r} via {key!r}")
    assert not missing, (
        "featured components reference a sibling without requiring it: " + "; ".join(missing)
    )


def test_apollo_esk_1_rtttl_and_battery_requires() -> None:
    body = orjson.loads((sb._BODIES_DIR / "apollo-esk-1.json").read_bytes())
    by_id = {fc["id"]: fc for fc in body["featured_components"]}
    assert by_id["rtttl_player"]["requires"] == ["buzzer_output"]
    assert by_id["battery_monitor"]["requires"] == ["i2c_bus"]
