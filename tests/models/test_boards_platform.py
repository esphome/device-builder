"""Platform-key alias handling for the deprecated rp2040 spelling."""

from __future__ import annotations

import pytest

from esphome_device_builder.models.boards import (
    RP2_PLATFORM_ALIASES,
    Platform,
    normalize_platform,
)


@pytest.mark.parametrize("name", ["rp2040", "RP2040", "Rp2040"])
def test_rp2040_folds_to_rp2(name: str) -> None:
    assert normalize_platform(name) == "rp2"


@pytest.mark.parametrize("name", ["rp2", "esp32", "esp8266", "bk72xx", "nrf52", ""])
def test_other_platforms_pass_through(name: str) -> None:
    assert normalize_platform(name) == name


def test_normalize_is_idempotent() -> None:
    assert normalize_platform(normalize_platform("rp2040")) == "rp2"


def test_aliases_cover_both_names() -> None:
    assert frozenset({"rp2", "rp2040"}) == RP2_PLATFORM_ALIASES


def test_platform_enum_accepts_both_rp2_spellings() -> None:
    assert Platform("rp2") is Platform.RP2
    assert Platform("rp2040") is Platform.RP2


def test_platform_enum_still_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="rp3"):
        Platform("rp3")
