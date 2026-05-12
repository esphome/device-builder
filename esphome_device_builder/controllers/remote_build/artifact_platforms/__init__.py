"""Per-platform build-tree inclusion lists for the remote-build artifact tarball.

Each module exposes ``TARGET_PLATFORM`` and ``BUILD_FILES``
(basenames under ``<build_path>/.pioenvs/<name>/``). The
libretiny variants re-export ``BUILD_FILES`` from
:mod:`._libretiny`.
"""

from __future__ import annotations

from . import bk72xx, esp32, esp8266, ln882x, nrf52, rp2040, rtl87xx

_PLATFORMS = (bk72xx, esp8266, esp32, ln882x, nrf52, rp2040, rtl87xx)

_BY_TARGET: dict[str, tuple[str, ...]] = {
    mod.TARGET_PLATFORM.lower(): mod.BUILD_FILES for mod in _PLATFORMS
}


def build_files_for_platform(target_platform: str) -> tuple[str, ...]:
    """Return basenames under ``.pioenvs/<name>/`` to ship for *target_platform*.

    Empty tuple for an unrecognised platform; the packer raises
    so a new platform can't silently ship an under-specified
    tarball. Lookup is case-insensitive because upstream
    StorageJSON stores ``target_platform.upper()``.
    """
    return _BY_TARGET.get(target_platform.lower(), ())
