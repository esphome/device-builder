"""Pin the importer's ``rgb_order`` / ``is_rgbw`` / ``is_wrgb`` fold into ``channel_colors``."""

from __future__ import annotations

from typing import Any

import pytest

from script.sync_esphome_devices import _extract_fields  # type: ignore[import-not-found]

_STRIP: dict[str, Any] = {
    "config_entries": [
        {"key": "channel_colors", "type": "string", "required": True},
        {"key": "rgb_order", "type": "string"},
        {"key": "is_rgbw", "type": "boolean"},
        {"key": "is_wrgb", "type": "boolean"},
        {"key": "num_leds", "type": "integer"},
    ]
}
_FASTLED: dict[str, Any] = {
    "config_entries": [
        {"key": "rgb_order", "type": "string"},
        {"key": "num_leds", "type": "integer"},
    ]
}


def _extract(item: dict[str, Any], component: dict[str, Any]) -> dict[str, Any] | None:
    return _extract_fields(item, component, {}, "light.esp32_rmt_led_strip")


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"rgb_order": "GRB", "num_leds": 10}, {"channel_colors": "GRB", "num_leds": 10}),
        (
            {"num_leds": 5, "rgb_order": "RGB", "is_wrgb": True},
            {"num_leds": 5, "channel_colors": "WRGB"},
        ),
        ({"channel_colors": "BGR", "rgb_order": "GRB"}, {"channel_colors": "BGR"}),
    ],
)
def test_legacy_keys_fold_into_channel_colors(
    item: dict[str, Any], expected: dict[str, Any]
) -> None:
    folded = _extract(item, _STRIP)
    assert folded == expected
    assert list(folded) == list(expected)


@pytest.mark.parametrize("item", [{"rgb_order": "GRBW"}, {"rgb_order": "GRB", "is_rgbw": "maybe"}])
def test_unfoldable_legacy_keys_drop_the_entry(item: dict[str, Any]) -> None:
    assert _extract(item, _STRIP) is None


def test_rgb_order_kept_without_channel_colors_entry() -> None:
    assert _extract({"rgb_order": "GRB", "num_leds": 4}, _FASTLED) == {
        "rgb_order": "GRB",
        "num_leds": 4,
    }
