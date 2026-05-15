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

import orjson
import pytest

from esphome_device_builder import definitions as defs
from esphome_device_builder.definitions import (
    _generic_image_url,
    _parse_connectivity,
    _parse_pin_features,
    _parse_tags,
    build_board_catalog_from_manifests,
    load_board_catalog,
)
from esphome_device_builder.models import (
    BoardCatalogEntry,
    BoardCatalogResponse,
    BoardEsphomeConfig,
    BoardTag,
    Connectivity,
    PinFeature,
    Platform,
)

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


def _write_fake_boards(root: Path) -> Path:
    """Create a minimal ``boards/`` tree with one good and one broken manifest."""
    fake_boards = root / "boards"
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
    return fake_boards


def test_build_from_manifests_skips_broken_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A manifest that fails to parse is skipped with a logged exception.

    Without ``strict=True`` a broken board must not abort the walk —
    the surviving manifests still need to render in any consumer that
    invokes the YAML walker directly (tests, ad-hoc scripts).
    """
    fake_boards = _write_fake_boards(tmp_path)
    monkeypatch.setattr(defs, "_BOARDS_DIR", fake_boards)
    # Co-relocate the generic-images dir so ``_generic_image_url``'s
    # ``_local_to_url`` (which calls ``relative_to(_BOARDS_DIR)``)
    # doesn't trip on the real generic SVGs being outside the fake
    # boards root. Empty dir → fallback returns "" cleanly.
    monkeypatch.setattr(defs, "_GENERIC_DIR", fake_boards / "_generic")

    with caplog.at_level(logging.ERROR):
        result = build_board_catalog_from_manifests()

    # Good board survived.
    ids = [b.id for b in result.boards]
    assert "good-board" in ids
    # Broken one was skipped, not crashed on.
    assert "broken-board" not in ids
    # Exception logged with the offending board's directory name.
    assert any(
        "broken-board" in rec.getMessage() for rec in caplog.records if rec.levelname == "ERROR"
    )


def test_build_from_manifests_strict_raises_on_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``strict=True`` re-raises the first per-board failure."""
    fake_boards = _write_fake_boards(tmp_path)
    monkeypatch.setattr(defs, "_BOARDS_DIR", fake_boards)
    monkeypatch.setattr(defs, "_GENERIC_DIR", fake_boards / "_generic")

    with pytest.raises(KeyError):
        build_board_catalog_from_manifests(strict=True)


def test_load_board_catalog_warns_when_json_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing ``boards.json`` returns an empty catalog and logs a warning."""
    monkeypatch.setattr(defs, "_BOARDS_JSON", tmp_path / "missing-boards.json")

    with caplog.at_level(logging.WARNING):
        result = load_board_catalog()

    assert result.boards == []
    assert any("boards.json" in rec.getMessage() for rec in caplog.records)


def test_load_board_catalog_reads_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``load_board_catalog`` deserializes the prebuilt JSON via mashumaro."""
    fixture = BoardCatalogResponse(
        boards=[
            BoardCatalogEntry(
                id="round-trip",
                name="Round Trip",
                description="JSON load test",
                manufacturer="",
                esphome=BoardEsphomeConfig(platform=Platform.ESP32, board="esp32dev"),
            ),
        ],
    )
    json_path = tmp_path / "boards.json"
    json_path.write_bytes(orjson.dumps(fixture.to_dict()))
    monkeypatch.setattr(defs, "_BOARDS_JSON", json_path)

    result = load_board_catalog()

    assert [b.id for b in result.boards] == ["round-trip"]
    assert result.boards[0].esphome.platform is Platform.ESP32


def test_load_board_catalog_handles_corrupt_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed ``boards.json`` returns an empty catalog instead of crashing startup."""
    json_path = tmp_path / "boards.json"
    json_path.write_bytes(b"{not valid json")
    monkeypatch.setattr(defs, "_BOARDS_JSON", json_path)

    with caplog.at_level(logging.ERROR):
        result = load_board_catalog()

    assert result.boards == []
    assert any(
        "boards.json" in rec.getMessage() for rec in caplog.records if rec.levelname == "ERROR"
    )


def test_load_default_component_rejects_non_string_non_dict_entry() -> None:
    """A malformed default_components entry (not str / dict) raises ``TypeError``.

    The schema validator keeps this from reaching runtime, but the
    loader still asserts the shape so a broken pre-commit (or a
    direct caller in tests / scripts) surfaces the error
    immediately instead of producing a half-built
    ``DefaultComponent``.
    """
    with pytest.raises(TypeError, match="default_components entry"):
        defs._load_default_component(123)
