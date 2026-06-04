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
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Recognizable, short, collision-safe roots (``esphb`` = ESPHome builder). The per-install
# suffix keeps two users / config dirs on one machine from sharing a junction.
_ROOT_PREFIX = "esphb"


@contextmanager
def windows_short_build_paths(config_dir: Path) -> Iterator[Path | None]:
    """Route the build tree through short Windows paths for the block.

    Yields the real short ``PLATFORMIO_CORE_DIR`` to thread onto app state, or ``None`` off
    Windows / when setup can't run.
    """
    if os.name != "nt":
        yield None
        return

    prior = os.environ.get("ESPHOME_DATA_DIR")
    real_data = Path(prior) if prior else config_dir / ".esphome"
    real_data.mkdir(parents=True, exist_ok=True)

    suffix = _suffix(real_data)
    data_junction = Path(f"C:\\{_ROOT_PREFIX}-{suffix}")
    pio_dir = Path(f"C:\\{_ROOT_PREFIX}-{suffix}-pio")

    try:
        _ensure_junction(data_junction, real_data)
        pio_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A locked-down host that refuses C:\ writes: leave the long default in place rather
        # than crash. Deep ESP-IDF builds may still overflow, but the dashboard runs.
        _LOGGER.warning("Could not set up short Windows build paths (%s); using defaults", exc)
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


def _suffix(real_data: Path) -> str:
    """Stable 4-hex per-install suffix from the real data dir (case-folded for Windows)."""
    key = str(real_data.resolve()).lower().encode()
    return hashlib.sha1(key).hexdigest()[:4]  # noqa: S324 — short non-crypto dir tag


def _ensure_junction(link: Path, target: Path) -> None:
    """Create directory junction ``link`` -> ``target`` if absent or pointing elsewhere.

    Synchronous on purpose: only runs at startup before the event loop, never from the
    running loop, so it needs no executor hop.
    """
    if link.exists():
        if os.path.realpath(link).lower() == os.path.realpath(target).lower():
            return
        link.rmdir()
    # ``mklink`` is a cmd builtin (no standalone exe); resolve COMSPEC so the executable is a
    # full path. ``close_fds=False`` mirrors helpers.subprocess and controllers.version_history.
    comspec = os.environ.get("COMSPEC", "cmd.exe")
    result = subprocess.run(  # noqa: S603
        [comspec, "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
        close_fds=False,
    )
    if result.returncode != 0:
        raise OSError(f"mklink /J {link} {target} failed: {result.stderr.strip()}")
