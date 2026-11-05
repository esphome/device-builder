"""The full-sync guard against unavailable or unrefined automation registries."""

from __future__ import annotations

import pytest

from script import sync_components  # type: ignore[import-not-found]

_ALL_KINDS = {"action", "condition", *sync_components._OPTIONAL_AUTOMATION_REGISTRIES}


def _registries(kinds: set[str] | frozenset[str]) -> dict[str, dict]:
    return {kind: {} for kind in kinds}


def _refined(kinds: set[str] | frozenset[str]) -> dict[str, dict]:
    return {kind: {"x": {}} for kind in kinds}


def test_missing_registry_import_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry that failed to import stops the sync and is named."""
    monkeypatch.setattr(
        sync_components,
        "_automation_registries",
        lambda: _registries(_ALL_KINDS - {"sensor.filter"}),
    )
    refined = _refined(sync_components._MUST_REFINE_KINDS)
    with pytest.raises(SystemExit, match=r"\['sensor\.filter'\] failed to import"):
        sync_components._fail_on_unrefined_registries(refined)


def test_unrefined_must_refine_kind_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A must-refine kind with no refinements stops the sync and is named."""
    monkeypatch.setattr(sync_components, "_automation_registries", lambda: _registries(_ALL_KINDS))
    refined = _refined(sync_components._MUST_REFINE_KINDS - {"light_effect"})
    with pytest.raises(SystemExit, match=r"\['light_effect'\] yielded no refinements"):
        sync_components._fail_on_unrefined_registries(refined)


def test_text_sensor_filters_need_no_refinements(monkeypatch: pytest.MonkeyPatch) -> None:
    """Text sensor filters may come back unrefined without stopping the sync."""
    monkeypatch.setattr(sync_components, "_automation_registries", lambda: _registries(_ALL_KINDS))
    refined = _refined(sync_components._MUST_REFINE_KINDS)
    sync_components._fail_on_unrefined_registries(refined)
