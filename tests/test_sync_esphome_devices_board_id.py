"""Board id canonicalization in ``_resolve_board_and_variant``."""

from __future__ import annotations

import pytest

from script.sync_boards import canonical_board_id  # type: ignore[import-not-found]
from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _resolve_board_and_variant,
)


@pytest.mark.parametrize(
    ("platform", "board", "expected"),
    [
        ("esp8266", "esp01-1m", "esp01_1m"),
        ("esp8266", "ESP01_1M", "esp01_1m"),
        ("esp8266", "nodemcuv2", "nodemcuv2"),
        ("esp32", "ESP32-S3-DevKitC-1", "esp32-s3-devkitc-1"),
        ("esp32", "esp32_s3_devkitc_1", "esp32-s3-devkitc-1"),
        ("esp8266", "not-a-real-board", "not-a-real-board"),
        ("not_a_platform", "esp01-1m", "esp01-1m"),
    ],
)
def test_canonical_board_id(platform: str, board: str, expected: str) -> None:
    """A misspelled board maps onto esphome's known id; anything unknown passes through."""
    assert canonical_board_id(platform, board) == expected


def test_resolve_board_canonicalizes_imported_board() -> None:
    """An imported ``board:`` typo lands in the manifest as the known id."""
    board, _, _ = _resolve_board_and_variant("esp8266", {"board": "esp01-1m"})
    assert board == "esp01_1m"
