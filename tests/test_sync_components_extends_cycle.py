"""Tests for the ``extends``-cycle guard and CORE priming in the catalog sync."""

from __future__ import annotations

import sys
from pathlib import Path

from esphome.const import KEY_CORE, KEY_TARGET_PLATFORM
from esphome.core import CORE

_SCRIPT_DIR = Path(__file__).parent.parent / "script"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import sync_components  # noqa: E402

# A field shape the converter treats as a nested sub-schema (``schema`` maps to
# ``nested`` in _TYPE_MAP; the inner ``schema`` is the recursion target).
_CYCLE_FIELD = {"key": "Optional", "type": "schema", "schema": {"extends": ["CYCLE"]}}


def test_convert_config_vars_breaks_self_referential_extends(monkeypatch) -> None:
    """A schema whose nested field re-extends its own base terminates (no RecursionError).

    Mirrors lvgl's ``WIDGET_TYPES`` cycle (a widget contains a widget list).
    """

    def fake_resolve(ref: str, schema_dir: Path) -> dict:
        # ``CYCLE`` resolves to a node carrying a nested field that extends
        # ``CYCLE`` again — an infinite loop without the _seen_refs guard.
        return {"child": dict(_CYCLE_FIELD)} if ref == "CYCLE" else {}

    monkeypatch.setattr(sync_components, "_resolve_extends", fake_resolve)

    entries = sync_components._convert_config_vars({"extends": ["CYCLE"]}, Path("/nonexistent"))

    # First expansion surfaces the child; its re-expansion of CYCLE is pruned.
    assert [e["key"] for e in entries] == ["child"]
    assert entries[0]["type"] == "nested"
    assert entries[0].get("config_entries") is None


def test_merge_extends_skips_seen_refs() -> None:
    """A ref already on the expansion path is not re-resolved."""
    node = {"extends": ["CYCLE"], "config_vars": {"local": {"key": "Optional"}}}
    merged = sync_components._merge_extends_config_vars(
        node, Path("/nonexistent"), frozenset({"CYCLE"})
    )
    assert set(merged) == {"local"}


def test_get_esphome_loader_primes_core_target_platform(monkeypatch) -> None:
    """The loader gives CORE a target_platform slot so import-time CORE.is_esp32 can't KeyError."""
    # Work on copies so the forced re-resolution + CORE mutation unwind on teardown.
    monkeypatch.setattr(CORE, "data", dict(CORE.data))
    monkeypatch.setitem(sync_components._ESPHOME_LOADER_CACHE, "resolved", False)
    monkeypatch.setitem(sync_components._ESPHOME_LOADER_CACHE, "module", None)
    CORE.data.pop(KEY_CORE, None)

    assert sync_components._get_esphome_loader() is not None
    assert KEY_TARGET_PLATFORM in CORE.data[KEY_CORE]
