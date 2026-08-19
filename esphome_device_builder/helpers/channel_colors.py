"""The led-strip ``rgb_order`` / ``is_rgbw`` / ``is_wrgb`` to ``channel_colors`` fold."""

from __future__ import annotations

# Stdlib-only: ``script/validate_definitions.py`` imports this from the
# pre-commit env, which has none of the package's dependencies.
from itertools import permutations

#: The led-strip keys ``channel_colors`` replaces.
CHANNEL_COLORS_LEGACY_KEYS = frozenset({"rgb_order", "is_rgbw", "is_wrgb"})

#: The deprecated ``rgb_order`` key's closed value set — every R/G/B
#: permutation; anything else must keep failing validation loudly.
_RGB_ORDERS = frozenset(map("".join, permutations("RGB")))


def fold_channel_colors_value(order: str, *, is_rgbw: bool, is_wrgb: bool) -> str | None:
    """``channel_colors`` for a legacy ``rgb_order`` + flags trio; ``None`` when unfoldable."""
    if is_rgbw and is_wrgb:
        return None
    value = order.upper()
    if value not in _RGB_ORDERS:
        return None
    if is_wrgb:
        return f"W{value}"
    if is_rgbw:
        return f"{value}W"
    return value
