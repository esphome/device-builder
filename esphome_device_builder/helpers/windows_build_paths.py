r"""
Windows short build paths: keep compile artefact paths under the 260-char ``MAX_PATH``.

On Windows the default build tree (``<config_dir>\.esphome\build\<name>\.pioenvs\<name>\...``)
plus the framework source paths CMake mirrors into ``CMakeFiles\<target>.dir\`` overflow
``MAX_PATH`` for deep ESP-IDF builds (libsodium / mbedtls), and the long framework include list
also overflows the Windows command-line limit at link time (issue #1190).

The fix routes the whole build tree through a short directory **junction** and points the
PlatformIO toolchain at a short **real** directory:

* ``ESPHOME_DATA_DIR`` -> junction ``C:\esphb-<suffix>`` into the real data dir. The junction
  *survives* PlatformIO/CMake path handling, so the compiler is handed the short string while
  the bytes stay under the real (config) dir, keeping uninstall clean. ``CORE.data_dir`` reads
  ``ESPHOME_DATA_DIR`` first, so the dashboard's own artefact reads and every compile subprocess
  resolve through the same short path with no divergence (see :mod:`helpers.storage_path`).
* ``PLATFORMIO_CORE_DIR`` -> real short ``C:\esphb-<suffix>-pio``. It must be a *real* dir, not a
  junction: ESP-IDF's ``idf.cmake`` REALPATHs ``IDF_PATH`` and resolves a junction back to its
  long target. Injected per-subprocess in
  :func:`controllers.firmware.cli.compose_subprocess_env`.

Empirically validated on a windows-latest runner (deepest path 229/206 vs 304 + "command line
too long" on the long default). No-op on every non-Windows platform.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Recognizable, short, collision-safe roots (``esphb`` = ESPHome builder). The per-install
# suffix keeps two users / config dirs on one machine from sharing a junction.
_ROOT_PREFIX = "esphb"


@dataclass
class _WindowsShortPathState:
    """Active short-path state; ``pio_core_dir`` is None when shortening is inactive."""

    pio_core_dir: Path | None = None


# Module-level mutable state (attribute mutation, no rebindable global).
_STATE = _WindowsShortPathState()


def apply_windows_short_build_paths(config_dir: Path) -> None:
    """Route the build tree through short Windows paths; no-op off Windows.

    Sets ``ESPHOME_DATA_DIR`` to a short junction into the real data dir and records a real
    short ``PLATFORMIO_CORE_DIR`` for :func:`windows_pio_core_dir`. Idempotent across restarts.
    """
    if os.name != "nt":
        return

    existing = os.environ.get("ESPHOME_DATA_DIR")
    real_data = Path(existing) if existing else config_dir / ".esphome"
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
        return

    os.environ["ESPHOME_DATA_DIR"] = str(data_junction)
    _STATE.pio_core_dir = pio_dir
    _LOGGER.info("Windows short build paths: %s -> %s, core %s", data_junction, real_data, pio_dir)


def windows_pio_core_dir() -> Path | None:
    """Return the real short ``PLATFORMIO_CORE_DIR``, or ``None`` if shortening is inactive."""
    return _STATE.pio_core_dir


def remove_windows_short_build_paths() -> None:
    """Drop the data junction (reparse point only; leaves the real data + toolchain)."""
    pio_core_dir = _STATE.pio_core_dir
    if os.name != "nt" or pio_core_dir is None:
        return
    junction = Path(str(pio_core_dir)[: -len("-pio")])
    try:
        if junction.exists():
            junction.rmdir()
    except OSError as exc:
        _LOGGER.debug("Could not remove junction %s: %s", junction, exc)
    _STATE.pio_core_dir = None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _suffix(real_data: Path) -> str:
    """Stable 4-hex per-install suffix from the real data dir (case-folded for Windows)."""
    key = str(real_data.resolve()).lower().encode()
    return hashlib.sha1(key).hexdigest()[:4]  # noqa: S324 — short non-crypto dir tag


def _ensure_junction(link: Path, target: Path) -> None:
    """Create directory junction ``link`` -> ``target`` if absent or pointing elsewhere.

    Synchronous on purpose: only called once at startup (before the event loop) and once at
    shutdown, never from the running loop, so it does not need an executor hop.
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
