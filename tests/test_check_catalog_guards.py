"""Pins ``check_catalog``'s full-catalog body guards, including failure branches."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from esphome_device_builder.controllers.components import ComponentCatalog
from script.check_catalog import (  # type: ignore[import-not-found]
    _GATING_FLOORS,
    _check_boolean_options_exclusive,
    _check_gating_floors,
    _load_body_from_disk,
)

pytestmark = pytest.mark.xdist_group("catalog")


def _entry(gate: str | None, children: list[Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(depends_on_component=gate, config_entries=children)


def _bodies_with(counts: dict[str, int]) -> list[tuple[str, Any]]:
    entries = [_entry(gate) for gate, n in counts.items() for _ in range(n)]
    return [("synthetic", SimpleNamespace(config_entries=entries))]


def test_floors_pass_at_the_floor() -> None:
    assert _check_gating_floors(_bodies_with(dict(_GATING_FLOORS))) == []


def test_floors_fail_one_below() -> None:
    counts = dict(_GATING_FLOORS)
    counts["mqtt"] -= 1
    failures = _check_gating_floors(_bodies_with(counts))
    assert len(failures) == 1
    assert "mqtt" in failures[0]


def test_floors_fail_loudly_on_an_empty_catalog() -> None:
    """The wholesale-loss scenario: no gates anywhere trips every floor."""
    failures = _check_gating_floors([("empty", SimpleNamespace(config_entries=[]))])
    assert len(failures) == len(_GATING_FLOORS)


def test_floor_tally_counts_nested_entries() -> None:
    n = _GATING_FLOORS["mqtt"]
    nested = _entry(None, children=[_entry("mqtt") for _ in range(n)])
    others = _bodies_with({k: v for k, v in _GATING_FLOORS.items() if k != "mqtt"})
    failures = _check_gating_floors([("nested", SimpleNamespace(config_entries=[nested])), *others])
    assert failures == []


def test_floor_tally_skips_missing_bodies() -> None:
    # The floors silently skip a None body; the sibling boolean/options
    # guard reports it, so the miss is covered when both run on the same
    # list (as main() feeds them).
    bodies = [("gone", None), *_bodies_with(dict(_GATING_FLOORS))]
    assert _check_gating_floors(bodies) == []


def test_floors_sit_in_a_sane_band_of_the_live_catalog(
    session_component_catalog: ComponentCatalog,
) -> None:
    """A floor typo (6000 -> 60, or above the live count) fails here, not in the field."""
    counts: dict[str, int] = {}
    for cid in session_component_catalog._by_id:
        component = _load_body_from_disk(cid)
        if component is None:
            continue
        stack = list(component.config_entries or [])
        while stack:
            entry = stack.pop()
            if entry.depends_on_component:
                counts[entry.depends_on_component] = counts.get(entry.depends_on_component, 0) + 1
            stack.extend(entry.config_entries or [])
    for gate, floor in _GATING_FLOORS.items():
        live = counts.get(gate, 0)
        assert floor <= live, (
            f"{gate} floor {floor} above live count {live}: the sync smoke would fire "
            "spuriously — lower _GATING_FLOORS in script/check_catalog.py"
        )
        assert floor >= live // 4, (
            f"{gate} floor {floor} drifted far below live count {live} — the catalog "
            "grew; bump _GATING_FLOORS in script/check_catalog.py"
        )


def _bool_entry(type_: str, options: list | None, children: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(key="k", type=type_, options=options, config_entries=children)


def test_boolean_options_exclusive_flags_the_union_regression() -> None:
    nested = _bool_entry("string", None, children=[_bool_entry("boolean", [{"value": "once"}])])
    bodies = [("comp", SimpleNamespace(config_entries=[nested]))]
    failures = _check_boolean_options_exclusive(bodies)
    assert len(failures) == 1
    assert "boolean entry carries an options list" in failures[0]


def test_boolean_options_exclusive_passes_clean_entries() -> None:
    bodies = [
        (
            "comp",
            SimpleNamespace(
                config_entries=[
                    _bool_entry("boolean", None),
                    _bool_entry("string", [{"value": "x"}]),
                ]
            ),
        )
    ]
    assert _check_boolean_options_exclusive(bodies) == []


def test_boolean_options_exclusive_reports_a_missing_body() -> None:
    assert _check_boolean_options_exclusive([("gone", None)]) == [
        "boolean/options check: missing body for gone"
    ]
