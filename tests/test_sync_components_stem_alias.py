"""Tests for the MDX bare-stem aliasing in ``script/sync_components.py``.

Pins ``_index_by_id_and_stem`` (the collision-aware id/stem index shared by the
four ``_load_mdx_*`` loaders) and the name-backfill it feeds, so a stem claimed
by two disagreeing docs pages (``image/animation`` vs ``lvgl/animation``) can't
rename the top-level ``animation`` component to "LVGL Animations".
"""

from __future__ import annotations

import pytest

import script.sync_components as sc  # type: ignore[import-not-found]
from script.sync_components import (  # type: ignore[import-not-found]
    _backfill_descriptions_from_mdx,
    _index_by_id_and_stem,
)


def test_exact_id_keys_are_authoritative() -> None:
    """Each catalog id maps to its own value regardless of shared stems."""
    out = _index_by_id_and_stem(
        [
            ("image.animation", "animation", "Animation"),
            ("lvgl.animation", "animation", "LVGL Animations"),
        ]
    )
    assert out["image.animation"] == "Animation"
    assert out["lvgl.animation"] == "LVGL Animations"


def test_ambiguous_stem_is_dropped() -> None:
    """Two pages disagreeing on a stem drop the bare-stem alias entirely."""
    out = _index_by_id_and_stem(
        [
            ("image.animation", "animation", "Animation"),
            ("lvgl.animation", "animation", "LVGL Animations"),
        ]
    )
    # The top-level ``animation`` id resolves only via this stem key; dropping
    # it lets the name-backfill keep the schema-derived name.
    assert "animation" not in out


def test_unambiguous_stem_is_aliased() -> None:
    """A stem owned by a single page still aliases so top-level ids find it."""
    out = _index_by_id_and_stem([("ota.esphome", "esphome", "ESPHome OTA Updates")])
    assert out["esphome"] == "ESPHome OTA Updates"


def test_matching_values_on_a_shared_stem_are_not_ambiguous() -> None:
    """Two pages agreeing on a stem keep the alias — no spurious drop."""
    out = _index_by_id_and_stem([("a.thing", "thing", "V"), ("b.thing", "thing", "V")])
    assert out["thing"] == "V"


def test_id_equal_to_stem_stays_authoritative_through_a_collision() -> None:
    """A top-level page's own id key survives a later same-stem collision."""
    out = _index_by_id_and_stem([("thing", "thing", "Top"), ("b.thing", "thing", "Other")])
    assert out["thing"] == "Top"


def _entry(component_id: str, name: str) -> dict:
    return {
        "id": component_id,
        "name": name,
        "description": "Animated images on displays.",
        "docs_url": "https://esphome.io/components/image/animation",
        "config_entries": [],
    }


@pytest.fixture
def _stub_mdx(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the docs-repo readers so backfill runs on injected dicts."""
    monkeypatch.setattr(sc, "_load_mdx_descriptions", dict)
    monkeypatch.setattr(sc, "_load_mdx_field_descriptions", dict)
    monkeypatch.setattr(sc, "_load_mdx_field_sections", dict)
    monkeypatch.setattr(sc, "_shared_docs_page_aliases", lambda _documented: {})


@pytest.mark.usefixtures("_stub_mdx")
def test_backfill_keeps_animation_name_when_stem_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the ambiguous ``animation`` stem gone, the name is left untouched."""
    # The fixed loader returns the image page under its exact id and no bare
    # ``animation`` alias (the collision dropped it).
    monkeypatch.setattr(sc, "_load_mdx_titles", lambda: {"image.animation": "Animation"})
    entries = [_entry("animation", "Animation")]
    _backfill_descriptions_from_mdx(entries)
    assert entries[0]["name"] == "Animation"


@pytest.mark.usefixtures("_stub_mdx")
def test_backfill_would_rename_when_stem_bleeds_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: a polluted ``animation`` stem key is what renamed it."""
    monkeypatch.setattr(sc, "_load_mdx_titles", lambda: {"animation": "LVGL Animations"})
    entries = [_entry("animation", "Animation")]
    _backfill_descriptions_from_mdx(entries)
    assert entries[0]["name"] == "LVGL Animations"
