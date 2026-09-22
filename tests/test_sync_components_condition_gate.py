"""The shipped action catalog carries ``has_condition_gate`` for the gated core actions."""

from __future__ import annotations

from esphome_device_builder.controllers.automations import catalog

_GATED = frozenset({"if", "while", "wait_until"})


def test_shipped_catalog_marks_the_gated_core_actions() -> None:
    assert {a.id for a in catalog.all_actions() if a.has_condition_gate} == _GATED


def test_gated_actions_are_control_flow() -> None:
    gated = [a for a in catalog.all_actions() if a.id in _GATED]
    assert len(gated) == len(_GATED)
    assert all(a.is_control_flow for a in gated)


def test_gated_body_matches_its_index_row() -> None:
    for action_id in _GATED:
        body = catalog.action_by_id(action_id)
        assert body is not None
        assert body.has_condition_gate is True
