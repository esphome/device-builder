"""Chip-vocabulary helpers importable without the models package.

The validate-definitions pre-commit hook runs without the package's
dependencies installed, so this module must stay import-light (stdlib only).
"""

from __future__ import annotations


def normalize_chip_variant(name: str) -> str:
    """Fold a chip-variant spelling (``ESP32-C3`` / ``esp32_c3``) onto the catalog form."""
    return name.strip().replace("-", "").replace("_", "").lower()


# ESPHome's LibreTiny board meta and the YAML's ``family:`` name the chip.
# Fold it into the per-chip series token (``mcu``): BK7231N/T/Q share one
# ``bk7231``, the rest map 1:1.
_LIBRETINY_FAMILY_MCU: dict[str, str] = {
    "BK7231N": "bk7231",
    "BK7231T": "bk7231",
    "BK7231Q": "bk7231",
}


def libretiny_family_mcu(family: str) -> str:
    """
    Fold a LibreTiny family (``RTL8720C``) onto its chip series token.

    A family outside the fold is its own lowercased token, so a new chip
    gets a distinct one the day ESPHome lists it.
    """
    folded = family.strip().upper()
    return _LIBRETINY_FAMILY_MCU.get(folded) or normalize_chip_variant(folded)
