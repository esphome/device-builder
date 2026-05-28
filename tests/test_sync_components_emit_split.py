"""
Tests for ``_emit_split_catalog``'s defensive guards.

Functional shape (write next_bodies, swap, atomic index replace) is
exercised end-to-end by every ``script/sync_components.py`` run in
CI; this file focuses on the targeted guards that don't have a
natural end-to-end witness.
"""

from __future__ import annotations

import pytest

import script.sync_components as sync_module
from script.sync_components import _emit_split_catalog  # type: ignore[import-not-found]


@pytest.mark.parametrize(
    "bad_id",
    [
        "../escape",
        "subdir/escape",
        "back\\slash",
        "null\x00byte",
        "",
    ],
)
def test_emit_split_catalog_refuses_traversal_shaped_id(
    bad_id: str,
    tmp_path,
    monkeypatch,
) -> None:
    """A catalog entry whose id has traversal-shaped chars hard-fails the build.

    Mirror of the runtime ``is_unsafe_component_id`` guard on the
    write side. Both ends of the on-disk catalog stay narrow
    against the same predicate so a sync-time bug or upstream
    schema change introducing a separator in an id can't silently
    escape ``definitions/components/``.
    """
    monkeypatch.setattr(sync_module, "_OUTPUT_BODIES_DIR", tmp_path / "components")
    monkeypatch.setattr(sync_module, "_OUTPUT_INDEX_FILE", tmp_path / "components.index.json")

    catalog = [
        {
            "id": bad_id,
            "name": "Trouble",
            "category": "misc",
            "config_entries": [],
        }
    ]

    with pytest.raises(ValueError, match="traversal-shaped"):
        _emit_split_catalog(catalog, "2026.5.1")
