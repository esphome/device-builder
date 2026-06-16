"""Per-platform build-tree inclusion lists for the remote-build artifact tarball.

Each module exposes ``TARGET_PLATFORM`` and ``BUILD_FILES``
(build-relative paths with ``{name}`` substitution). The
libretiny variants re-export ``BUILD_FILES`` from
:mod:`._libretiny`. ESP32 chip variants (``ESP32S3``,
``ESP32C3``, …) all fold to the ``esp32`` module.
"""

from __future__ import annotations

from functools import cache

from ....definitions import load_platform_capabilities_index
from . import bk72xx, esp32, esp8266, ln882x, nrf52, rp2040, rtl87xx

_PLATFORMS = (bk72xx, esp8266, esp32, ln882x, nrf52, rp2040, rtl87xx)


@cache
def _by_target() -> dict[str, tuple[str, ...]]:
    """Map ``target_platform`` -> BUILD_FILES, ESP32 chip variants folded to esp32.

    StorageJSON stores variants (``ESP32S3``, ``ESP32C3``, …) as
    ``target_platform``; they all build through the umbrella ``esp32`` component.
    The variant list comes from the generated index rather than
    ``esphome.components.esp32`` so this import stays off cold start.
    """
    by_target = {mod.TARGET_PLATFORM.lower(): mod.BUILD_FILES for mod in _PLATFORMS}
    for variant in load_platform_capabilities_index().esp32_variants:
        by_target.setdefault(variant.lower(), esp32.BUILD_FILES)
    return by_target


def build_files_for_platform(target_platform: str) -> tuple[str, ...]:
    """Return BUILD_FILES for *target_platform*; empty tuple if unrecognised."""
    key = target_platform.lower()
    files = _by_target().get(key)
    if files is not None:
        return files
    # Mirror download.py's _resolve_download_component esp32 fold, so an esp32
    # variant still resolves on a degraded (empty) index and an offload packs
    # rather than raising on empty build_files.
    if key.startswith("esp32"):
        return esp32.BUILD_FILES
    return ()


# Prime the cached map at import so the first artifact build doesn't pay the
# (small, esphome-free) index read inside the event loop.
_by_target()
