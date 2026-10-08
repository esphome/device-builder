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
        ("esp8266", "ESP01-1M", "esp01_1m"),
        ("esp32", "esp32_s3_devkitc_1", "esp32-s3-devkitc-1"),
        ("esp32", "HELTEC_WIFI_LORA_32_V3", "heltec_wifi_lora_32_V3"),
        ("rp2040", "RPIPICOW", "rpipicow"),
        ("esp8266", "not-a-real-board", "not-a-real-board"),
    ],
)
def test_canonical_board_id(platform: str, board: str, expected: str) -> None:
    """A case or separator variant maps onto esphome's id; an unknown board passes through."""
    assert canonical_board_id(platform, board) == expected


def test_resolve_board_canonicalizes_imported_board() -> None:
    """An imported ``board:`` typo lands in the manifest as esphome's id."""
    board, _, _ = _resolve_board_and_variant("esp8266", {"board": "esp01-1m"})
    assert board == "esp01_1m"
