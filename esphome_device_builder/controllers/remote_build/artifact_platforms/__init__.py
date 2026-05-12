"""Per-platform build-tree inclusion lists for the remote-build artifact tarball.

Each module exposes ``TARGET_PLATFORM`` and ``BUILD_FILES``
(build-relative paths with ``{name}`` substitution). The
libretiny variants re-export ``BUILD_FILES`` from
:mod:`._libretiny`. ESP32 chip variants (``ESP32S3``,
``ESP32C3``, …) all fold to the ``esp32`` module.
"""

from __future__ import annotations

from esphome.components.esp32 import VARIANTS as _ESP32_VARIANTS

from . import bk72xx, esp32, esp8266, ln882x, nrf52, rp2040, rtl87xx

_PLATFORMS = (bk72xx, esp8266, esp32, ln882x, nrf52, rp2040, rtl87xx)

_BY_TARGET: dict[str, tuple[str, ...]] = {
    mod.TARGET_PLATFORM.lower(): mod.BUILD_FILES for mod in _PLATFORMS
}

# ESP32 chip variants StorageJSON stores as ``target_platform``
# (``ESP32S3``, ``ESP32C3``, ``ESP32H2``, …) all build through the
# umbrella ``esp32`` component, so resolve them to the same module.
# Sourced from upstream so a new variant lands here without an
# edit. The base ``"esp32"`` is already in ``_BY_TARGET``.
for _variant in _ESP32_VARIANTS:
    _BY_TARGET.setdefault(_variant.lower(), esp32.BUILD_FILES)


def build_files_for_platform(target_platform: str) -> tuple[str, ...]:
    """Return BUILD_FILES for *target_platform*; empty tuple if unrecognised."""
    return _BY_TARGET.get(target_platform.lower(), ())
