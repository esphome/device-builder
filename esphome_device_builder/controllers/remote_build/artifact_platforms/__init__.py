"""
Per-platform build-tree inclusion lists for the remote-build artifact tarball.

Each module here exposes ``TARGET_PLATFORM`` (the canonical
``StorageJSON.target_platform`` value) and ``BUILD_FILES`` (a
tuple of ``<build_path>``-relative paths the offloader needs to
flash). Adding a new platform = one new module + one import line
below.

The libretiny family (``bk72xx`` / ``rtl87xx`` / ``ln882x``)
re-exports a shared :data:`BUILD_FILES` from :mod:`._libretiny`
so the three platforms can carry their own ``TARGET_PLATFORM``
while sharing one inclusion tuple — divergence (if it ever
happens) costs replacing the alias with a local tuple.

Platform-independent metadata files (``platformio.ini``,
``<data_dir>/idedata/<name>.json``,
``<data_dir>/storage/<basename>.json``) ride alongside the
build tree at fixed top-level tarball names — see
:func:`controllers.remote_build.artifacts_tarball.pack_build_artifacts`.
The per-platform modules only carry the build-tree slice.
"""

from __future__ import annotations

from . import bk72xx, esp32, esp8266, ln882x, nrf52, rp2040, rtl87xx

_PLATFORMS = (bk72xx, esp8266, esp32, ln882x, nrf52, rp2040, rtl87xx)

_BY_TARGET: dict[str, tuple[str, ...]] = {
    mod.TARGET_PLATFORM.lower(): mod.BUILD_FILES for mod in _PLATFORMS
}


def build_files_for_platform(target_platform: str) -> tuple[str, ...]:
    """Return the build-relative files to include for *target_platform*.

    Empty tuple for an unrecognised platform. The packer treats
    that as a structural failure and raises so a new platform
    can't silently ship an under-specified tarball.

    *target_platform* is matched case-insensitively against each
    module's ``TARGET_PLATFORM`` — upstream's StorageJSON
    serialises ``esph.target_platform.upper()``
    (``storage_json.py:160``) so on-disk sidecars carry
    uppercased values, but lowercase is the canonical form
    everywhere else in the dashboard.
    """
    return _BY_TARGET.get(target_platform.lower(), ())
