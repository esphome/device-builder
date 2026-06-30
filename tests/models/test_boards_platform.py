"""Platform-key alias handling for the rp2040 -> rp2 rename."""

from __future__ import annotations

import pytest

from esphome_device_builder.models.boards import (
    RP2_PLATFORM_ALIASES,
    normalize_platform,
)


@pytest.mark.parametrize("name", ["rp2", "RP2", "Rp2"])
def test_rp2_folds_to_rp2040(name: str) -> None:
    assert normalize_platform(name) == "rp2040"


@pytest.mark.parametrize("name", ["rp2040", "esp32", "esp8266", "bk72xx", "nrf52", ""])
def test_other_platforms_pass_through(name: str) -> None:
    assert normalize_platform(name) == name


def test_normalize_is_idempotent() -> None:
    assert normalize_platform(normalize_platform("rp2")) == "rp2040"


def test_aliases_cover_both_names() -> None:
    assert frozenset({"rp2", "rp2040"}) == RP2_PLATFORM_ALIASES
