"""Coverage for ``DevicesController._derive_board_id`` (catalog lookup order)."""

from __future__ import annotations

from pathlib import Path

from .conftest import MakeControllerFactory, StubBoardLookups


def test_derive_returns_empty_when_boards_catalog_unloaded(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """``boards is None`` (catalog still loading) → empty string."""
    controller = make_controller(tmp_path, with_boards=False)
    controller._db.boards = None

    assert controller._derive_board_id("esp32", "esp32dev", "") == ""


def test_derive_uses_pio_board_match_first(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """A ``board:`` match wins; the platform fallback is not reached."""
    controller = make_controller(tmp_path, with_boards=True)
    boards = StubBoardLookups(controller)
    pio_lookup = boards.find_by_pio_board_returns("generic-esp32c3")
    platform_lookup = boards.find_by_platform_variant_returns(None)

    result = controller._derive_board_id("esp32", "esp32-c3-devkitm-1", "ESP32C3")

    assert result == "generic-esp32c3"
    pio_lookup.assert_called_once_with(
        "esp32-c3-devkitm-1", "ESP32C3", "esp32", prefer_exact_id=True
    )
    platform_lookup.assert_not_called()


def test_derive_falls_back_to_platform_when_pio_board_misses(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """An uncatalogued ``board:`` falls back to the platform + variant lookup."""
    controller = make_controller(tmp_path, with_boards=True)
    boards = StubBoardLookups(controller)
    pio_lookup = boards.find_by_pio_board_returns(None)
    platform_lookup = boards.find_by_platform_variant_returns("generic-esp32s3")

    result = controller._derive_board_id("esp32", "unknown-board", "ESP32S3")

    assert result == "generic-esp32s3"
    pio_lookup.assert_called_once_with("unknown-board", "ESP32S3", "esp32", prefer_exact_id=True)
    platform_lookup.assert_called_once_with("esp32", "ESP32S3")


def test_derive_skips_pio_board_without_board(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """No ``board:`` → straight to the platform lookup."""
    controller = make_controller(tmp_path, with_boards=True)
    boards = StubBoardLookups(controller)
    pio_lookup = boards.find_by_pio_board_returns(None)
    platform_lookup = boards.find_by_platform_variant_returns("generic-esp32")

    assert controller._derive_board_id("esp32", "", "") == "generic-esp32"
    pio_lookup.assert_not_called()
    platform_lookup.assert_called_once_with("esp32", "")


def test_derive_returns_empty_when_no_catalog_entry_matches(
    tmp_path: Path, make_controller: MakeControllerFactory
) -> None:
    """Both lookups miss → empty string."""
    controller = make_controller(tmp_path, with_boards=True)
    boards = StubBoardLookups(controller)
    boards.find_by_pio_board_returns(None)
    boards.find_by_platform_variant_returns(None)

    assert controller._derive_board_id("esp32", "nonexistent-board", "") == ""
