"""Variant inference from the board id in ``_resolve_board_and_variant``."""

from __future__ import annotations

import pytest

from script.sync_esphome_devices import (  # type: ignore[import-not-found]
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
