"""Tests for schema-derived per-platform defaults (``cv.SplitDefault``)."""

from __future__ import annotations

import esphome.config_validation as cv

from script.sync_components import (  # type: ignore[import-not-found]
    _collect_platform_defaults,
)


class _FakeManifest:
    """Minimal manifest stub — only ``config_schema`` is read."""

    def __init__(self, schema: object) -> None:
        self.config_schema = schema


def test_collect_top_level_split_default() -> None:
    """A ``cv.SplitDefault`` key surfaces its per-platform default map."""
    schema = {cv.SplitDefault("tx_buffer_size", esp32=512, esp8266=128): cv.int_}
    out = _collect_platform_defaults(_FakeManifest(schema))
    assert out == {("tx_buffer_size",): {"esp32": 512, "esp8266": 128}}


def test_collect_split_default_inside_list_item_schema() -> None:
    """A ``cv.SplitDefault`` inside a ``cv.ensure_list`` item keeps its path."""
    schema = {
        cv.Optional("entries"): cv.ensure_list(
            {cv.SplitDefault("mode", esp32="a", esp8266="b"): cv.string}
        ),
    }
    out = _collect_platform_defaults(_FakeManifest(schema))
    assert out == {("entries", "mode"): {"esp32": "a", "esp8266": "b"}}
