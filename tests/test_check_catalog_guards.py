"""Pins ``script/check_catalog.py``'s gating guards, including their failure branches."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from script.check_catalog import (  # type: ignore[import-not-found]
    _GATING_FLOORS,
    _check_gating_floors,
)


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
    bodies = [("gone", None), *_bodies_with(dict(_GATING_FLOORS))]
    assert _check_gating_floors(bodies) == []
