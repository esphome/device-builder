"""Chip-vocabulary helpers importable without the models package.

The validate-definitions pre-commit hook runs without the package's
dependencies installed, so this module must stay import-light (stdlib only).
"""

from __future__ import annotations


def normalize_chip_variant(name: str) -> str:
    """Fold a chip-variant spelling (``ESP32-C3`` / ``esp32_c3``) onto the catalog form."""
    return name.strip().replace("-", "").replace("_", "").lower()
