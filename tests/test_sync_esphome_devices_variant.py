"""Variant inference from the board id in ``_resolve_board_and_variant``."""

from __future__ import annotations

import pytest
from esphome.components.esp32.boards import BOARDS

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
    _ESP32_VARIANT_DEFAULT_BOARD,
    _resolve_board_and_variant,
)


@pytest.mark.parametrize(
    ("board", "expected"),
    [
        ("esp32-p4-evboard", "esp32p4"),
        ("waveshare_esp32_p4_eth", "esp32p4"),  # underscore separators must work
        ("esp32_s3_zero", "esp32s3"),
        ("esp32-c61-devkitc1", "esp32c61"),  # longest-first: c61, not c6
        ("esp32-s3-devkitc-1", "esp32s3"),
        ("esp32dev", None),  # classic esp32 — no variant suffix
        ("esp32-poe", None),
    ],
)
def test_infers_variant_from_board_id(board: str, expected: str | None) -> None:
    _, variant, _ = _resolve_board_and_variant("esp32", {"board": board})
    assert variant == expected


def test_explicit_variant_wins_over_inference() -> None:
    """A page's own ``variant:`` takes precedence over board-id inference."""
    _, variant, _ = _resolve_board_and_variant(
        "esp32", {"board": "esp32-p4-evboard", "variant": "ESP32S3"}
    )
    assert variant == "esp32s3"


def test_variant_default_boards_exist_in_esphome() -> None:
    """Every ``_ESP32_VARIANT_DEFAULT_BOARD`` value is a real ESPHome board id.

    A bogus default (``esp32-p4-function-ev-board``) shipped in an imported
    manifest unnoticed; the id must resolve in ``BOARDS`` so variant-only
    upstream pages import a buildable board.
    """
    missing = {v: b for v, b in _ESP32_VARIANT_DEFAULT_BOARD.items() if b not in BOARDS}
    assert not missing, missing


def test_p4_variant_default_board_is_pre_rev3() -> None:
    """The esp32p4 default stays pre-rev3: rev3-min firmware faults at boot on that silicon."""
    board = _ESP32_VARIANT_DEFAULT_BOARD["esp32p4"]
    assert BOARDS[board].get("engineering_sample") is True
