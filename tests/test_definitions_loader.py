"""Coverage for ``definitions/__init__.py`` error / fallback branches.

The board catalog loader has a handful of "log and skip" branches
that the happy-path catalog tests don't reach because the bundled
board manifests are well-formed by definition. These tests target
those branches directly so a refactor that changed the
warning-on-unknown-X policy (e.g. raising instead) would surface.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from esphome_device_builder import definitions as defs
from esphome_device_builder.definitions import (
    _generic_image_url,
    _parse_connectivity,
    _parse_pin_features,
    _parse_tags,
    load_board_catalog,
)
from esphome_device_builder.models import BoardTag, Connectivity, PinFeature

_DEFS_MOD = "esphome_device_builder.definitions"


def test_generic_image_url_returns_empty_when_no_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """No variant svg AND no platform svg → empty string.

    Pin the ``return ""`` fallback. The catalog loader uses the
    empty-string sentinel to fall through to the auto-discovered
    images list — a regression that returned a placeholder URL
    or raised would either leak a broken image into the wire
    response or crash the catalog load on every unknown chip.
    """
    # Point the generic-images dir at an empty tmp dir so neither
    # the variant nor platform svg can exist.
    empty_dir = Path("/nonexistent-generic-dir-for-test")
    monkeypatch.setattr(defs, "_GENERIC_DIR", empty_dir)

    # Variant explicitly set; both lookups miss.
    assert _generic_image_url("esp32", "esp32s99") == ""
    # No variant; platform lookup also misses.
    assert _generic_image_url("totally-fake-platform", None) == ""


def test_parse_pin_features_logs_and_skips_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown pin feature is dropped with a warning, not a hard error.

    Manifests are hand-curated YAML (not auto-generated), so
    typos are likely. Failing the catalog load on every typo
    would break the whole dashboard — silently skipping is the
    pragmatic call, but we still need the warning so the
    operator can find the typo.
    """
    with caplog.at_level(logging.WARNING):
        result = _parse_pin_features(
            ["adc", "dac", "totally_made_up_feature"], board_id="my-board", gpio=21
        )

    assert result == [PinFeature.ADC, PinFeature.DAC]
    # Warning fired once with the offending value, board id, and pin.
    matching = [
        rec
        for rec in caplog.records
        if "totally_made_up_feature" in rec.getMessage()
        and "my-board" in rec.getMessage()
        and "21" in rec.getMessage()
    ]
    assert matching, [rec.getMessage() for rec in caplog.records]


def test_parse_tags_logs_and_skips_unknown(caplog: pytest.LogCaptureFixture) -> None:
    """An unknown tag is dropped with a warning."""
    with caplog.at_level(logging.WARNING):
        result = _parse_tags(["compact", "made-up-tag", "dev-kit"], board_id="my-board")

    assert result == [BoardTag.COMPACT, BoardTag.DEV_KIT]
    assert any(
        "made-up-tag" in rec.getMessage() and "my-board" in rec.getMessage()
        for rec in caplog.records
    )


def test_parse_connectivity_logs_and_skips_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown connectivity value is dropped with a warning."""
    with caplog.at_level(logging.WARNING):
        result = _parse_connectivity(["wifi", "bogus-wireless"], board_id="my-board")

    assert result == [Connectivity.WIFI]
    assert any(
        "bogus-wireless" in rec.getMessage() and "my-board" in rec.getMessage()
        for rec in caplog.records
    )


def test_load_board_catalog_skips_broken_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A manifest that fails to parse is skipped with a logged exception.

    Pin the outer ``except`` in ``load_board_catalog``. A single
    broken manifest must not crash the whole catalog load — the
    user's other working boards still need to render. Without
    the catch, every dashboard load that hit the broken file
    would 500-out with no working device drawer.
    """
    fake_boards = tmp_path / "boards"
    fake_boards.mkdir()
    # One good manifest.
    (fake_boards / "good-board").mkdir()
    (fake_boards / "good-board" / "manifest.yaml").write_text(
        "id: good-board\n"
        "name: Good Board\n"
        "description: Works fine\n"
        "esphome:\n"
        "  platform: esp32\n"
        "  board: esp32dev\n",
        encoding="utf-8",
    )
    # One that's missing the required ``esphome`` key.
    (fake_boards / "broken-board").mkdir()
    (fake_boards / "broken-board" / "manifest.yaml").write_text(
        "id: broken-board\nname: Broken\ndescription: Missing esphome key\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(defs, "_BOARDS_DIR", fake_boards)
    # Co-relocate the generic-images dir so ``_generic_image_url``'s
    # ``_local_to_url`` (which calls ``relative_to(_BOARDS_DIR)``)
    # doesn't trip on the real generic SVGs being outside the fake
    # boards root. Empty dir → fallback returns "" cleanly.
    monkeypatch.setattr(defs, "_GENERIC_DIR", fake_boards / "_generic")

    with caplog.at_level(logging.ERROR):
        result = load_board_catalog()

    # Good board survived.
    ids = [b.id for b in result.boards]
    assert "good-board" in ids
    # Broken one was skipped, not crashed on.
    assert "broken-board" not in ids
    # Exception logged with the offending board's directory name.
    assert any(
        "broken-board" in rec.getMessage() for rec in caplog.records if rec.levelname == "ERROR"
    )
