r"""
Windows short build paths: keep compile artefact paths under the 260-char ``MAX_PATH``.

On Windows the default build tree plus the framework source paths CMake mirrors into
``CMakeFiles\<target>.dir\`` overflow ``MAX_PATH`` for deep ESP-IDF builds, and the long
framework include list also overflows the command-line limit at link time.

:func:`windows_short_build_paths` routes the build tree through a short directory **junction**
and yields the short PlatformIO core dir, for the duration of the ``with`` block:

* ``ESPHOME_DATA_DIR`` -> junction ``C:\esphb-<suffix>`` into the real data dir. The junction
  survives PlatformIO/CMake path handling, so the compiler gets the short string while the
  bytes stay under the real (config) dir. ``CORE.data_dir`` reads ``ESPHOME_DATA_DIR`` first,
  so the dashboard's own artefact reads and every compile subprocess resolve through the same
  short path with no divergence (see :mod:`helpers.storage_path`).
* The yielded ``PLATFORMIO_CORE_DIR`` is a real short ``C:\esphb-<suffix>-pio``. It must be a
  *real* dir, not a junction: ESP-IDF's ``idf.cmake`` REALPATHs ``IDF_PATH`` and resolves a
  junction back to its long target. The caller threads it onto app state and
  :func:`controllers.firmware.cli.compose_subprocess_env` injects it per-subprocess.

The junction and the real toolchain dir are left on disk on exit (a concurrent dashboard may
share them; the desktop uninstaller reclaims ``C:\esphb-*``); only the ``ESPHOME_DATA_DIR``
override is restored. No-op on every non-Windows platform (yields ``None``).
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Recognizable short root (``esphb`` = ESPHome builder). The per-install suffix keeps two
# users / config dirs on one machine from sharing a junction.
_ROOT_PREFIX = "esphb"

# Drive root the short junction + toolchain dirs live under. A module attribute so tests can
# point it at a tmp dir instead of ``C:\``.
_JUNCTION_ROOT = Path("C:\\")


@contextmanager
def windows_short_build_paths(config_dir: Path) -> Iterator[Path | None]:
    """Route the build tree through short Windows paths for the block.

    Yields the real short ``PLATFORMIO_CORE_DIR`` to thread onto app state, or ``None`` off
    Windows / when setup can't run.
    """
    if not _is_windows():
        yield None
        return

    prior = os.environ.get("ESPHOME_DATA_DIR")
    real_data = Path(prior) if prior else config_dir / ".esphome"
    real_data.mkdir(parents=True, exist_ok=True)

    suffix = _suffix(real_data)
    data_junction = _JUNCTION_ROOT / f"{_ROOT_PREFIX}-{suffix}"
    pio_dir = _JUNCTION_ROOT / f"{_ROOT_PREFIX}-{suffix}-pio"

    try:
        _ensure_junction(data_junction, real_data)
        pio_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # A locked-down host (or, vanishingly rarely, a suffix collision) leaves the long
        # default in place. Logged at error so a later MAX_PATH compile failure can be traced
        # back to this setup miss rather than read as a mysterious compiler error.
        _LOGGER.exception(
            "Could not set up short Windows build paths; deep ESP-IDF builds may overflow MAX_PATH"
        )
        yield None
        return

    os.environ["ESPHOME_DATA_DIR"] = str(data_junction)
    _LOGGER.info("Windows short build paths: %s -> %s, core %s", data_junction, real_data, pio_dir)
    try:
        yield pio_dir
    finally:
        # Restore the override; leave the junction + toolchain on disk (a concurrent dashboard
        # may share them; the uninstaller reclaims them).
        if prior is None:
            os.environ.pop("ESPHOME_DATA_DIR", None)
        else:
            os.environ["ESPHOME_DATA_DIR"] = prior


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    """Whether the short-path machinery applies (split out so tests can drive the nt branch)."""
    return os.name == "nt"


def _suffix(real_data: Path) -> str:
    """Stable 8-hex per-install suffix from the real data dir (case-folded for Windows).

    8 hex chars (~32 bits) keeps the chance that two different config dirs collide onto the
    same junction name negligible, so the collision fallback in :func:`_ensure_junction`
    almost never fires.
    """
    key = str(real_data.resolve()).lower().encode()
    return hashlib.sha1(key).hexdigest()[:8]  # noqa: S324 — short non-crypto dir tag


def _ensure_junction(link: Path, target: Path) -> None:
    """Create directory junction ``link`` -> ``target`` if absent.

    Reuses an existing junction already pointing at *target* (our own, from a prior run). If
    the name is held by a junction pointing elsewhere (a suffix collision with another config
    dir), raises ``OSError`` so the caller falls back to long paths rather than stealing a
    junction a concurrent instance may be building under.

    Synchronous on purpose: only runs at startup before the event loop, never from the running
    loop, so it needs no executor hop.
    """
    if link.exists():
        if os.path.realpath(link).lower() == os.path.realpath(target).lower():
            return
        msg = f"{link} already exists pointing elsewhere"
        raise OSError(msg)
    _create_junction(link, target)


def _create_junction(link: Path, target: Path) -> None:
    """Create a directory junction at *link* pointing to *target* via the Windows native API.

    Uses ``_winapi.CreateJunction`` rather than ``cmd /c mklink`` so a space-bearing target
    (common Windows profile dirs) can't be mangled by cmd.exe's quote-stripping.
    """
    import _winapi  # noqa: PLC0415 — Windows-only; imported lazily so off-Windows never loads it

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]
