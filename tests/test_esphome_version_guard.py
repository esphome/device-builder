"""Pin the shared ESPHome-version guard used by both catalog sync scripts."""

from __future__ import annotations

import esphome.const
import pytest

from script._esphome_version import assert_installed_esphome


def test_exact_match_passes(monkeypatch):
    monkeypatch.setattr(esphome.const, "__version__", "2026.6.2")
    assert_installed_esphome("2026.6.2", what="t")  # no raise


def test_mismatch_raises_with_install_hint(monkeypatch):
    monkeypatch.setattr(esphome.const, "__version__", "2026.4.3")
    with pytest.raises(SystemExit, match=r"esphome==2026\.6\.2"):
        assert_installed_esphome("2026.6.2", what="sync_components")


def test_normalize_matches_beta_to_base(monkeypatch):
    monkeypatch.setattr(esphome.const, "__version__", "2026.7.0b1")
    assert_installed_esphome("2026.7.0", what="t", normalize=lambda v: v.split("b")[0])  # no raise


def test_alt_fix_appended_on_mismatch(monkeypatch):
    monkeypatch.setattr(esphome.const, "__version__", "1.0.0")
    with pytest.raises(SystemExit, match="run a full sync instead"):
        assert_installed_esphome("2.0.0", what="t", alt_fix="run a full sync instead")


def test_not_importable_message(monkeypatch):
    monkeypatch.delattr(esphome.const, "__version__")
    with pytest.raises(SystemExit, match="not importable"):
        assert_installed_esphome("2026.6.2", what="t")


def test_sync_components_main_requires_exact_match(monkeypatch):
    # The guard runs before any schema fetch / esphome.components import, so a
    # mismatch raises with no network. A base release does NOT satisfy a beta
    # install here: components match exactly (unlike the board catalog), so this
    # pins that main() wires the guard without a canonicalizing normalize.
    # Imported lazily so the lightweight helper tests above don't pay the
    # heavy sync_components import at collection.
    from script import sync_components  # noqa: PLC0415

    monkeypatch.setattr("sys.argv", ["sync_components.py", "--version", "2026.6.2"])
    monkeypatch.setattr(esphome.const, "__version__", "2026.6.2b1")
    with pytest.raises(SystemExit, match=r"needs ESPHome 2026\.6\.2"):
        sync_components.main()
