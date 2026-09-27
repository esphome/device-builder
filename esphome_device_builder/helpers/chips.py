"""Chip-vocabulary helpers importable without the models package.

The validate-definitions pre-commit hook runs without the package's
dependencies installed, so this module must stay import-light (stdlib only).
"""

from __future__ import annotations

import re


def normalize_chip_variant(name: str) -> str:
    """Fold a chip-variant spelling (``ESP32-C3`` / ``esp32_c3``) onto the catalog form."""
    return name.strip().replace("-", "").replace("_", "").lower()


# ESPHome's LibreTiny board meta and the YAML's ``family:`` name the chip.
# Fold it into the per-chip series token (``mcu``): BK7231N/T/Q share one
# ``bk7231``, the rest map 1:1.
LIBRETINY_FAMILY_MCU: dict[str, str] = {
    "BK7231N": "bk7231",
    "BK7231T": "bk7231",
    "BK7231Q": "bk7231",
    "BK7238": "bk7238",
    "BK7251": "bk7251",
    "RTL8710B": "rtl8710b",
    "RTL8720C": "rtl8720c",
    "LN882H": "ln882h",
}


def libretiny_family_mcu(family: str) -> str:
    """
    Fold a LibreTiny family (``RTL8720C``) onto its chip series token.

    An unmapped future family falls back to its own lowercased token, so a
    new chip still gets a distinct one.
    """
    folded = family.strip().upper()
    return LIBRETINY_FAMILY_MCU.get(folded) or re.sub(r"[^a-z0-9]", "", folded.lower())
