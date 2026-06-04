r"""
Keep Windows compile paths under the 260-char ``MAX_PATH`` for deep ESP-IDF builds.

:func:`windows_short_build_paths` points ``ESPHOME_DATA_DIR`` at a short junction
(``C:\esphb-<suffix>``) for the ``with`` block and yields a real short ``PLATFORMIO_CORE_DIR``.
The data dir is a junction (it survives PlatformIO/CMake path handling); the core dir must be a
*real* dir, since ESP-IDF REALPATHs ``IDF_PATH`` and would resolve a junction back to its long
target. The junction + toolchain stay on disk on exit (shared across runs; not cleaned on
esphome-desktop uninstall today). No-op off Windows.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fnv_hash_fast import fnv1a_32

_LOGGER = logging.getLogger(__name__)

_ROOT_PREFIX = "esphb"

# Drive root for the junction + toolchain dirs; a module attribute so tests can repoint it.
_JUNCTION_ROOT = Path("C:\\")


@contextmanager
def windows_short_build_paths(config_dir: Path) -> Iterator[Path | None]:
    """Yield the real short ``PLATFORMIO_CORE_DIR`` for the block, or ``None`` off Windows."""
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
        # Logged at error so a later MAX_PATH compile failure traces back to this setup miss.
        _LOGGER.exception("Could not set up short Windows build paths; using defaults")
        yield None
        return

    os.environ["ESPHOME_DATA_DIR"] = str(data_junction)
    _LOGGER.info("Windows short build paths: %s -> %s, core %s", data_junction, real_data, pio_dir)
    try:
        yield pio_dir
    finally:
        # Restore only the env override; the junction + toolchain stay (a concurrent dashboard
        # may share them).
        if prior is None:
            os.environ.pop("ESPHOME_DATA_DIR", None)
        else:
            os.environ["ESPHOME_DATA_DIR"] = prior


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    """Whether the short-path machinery applies (a seam tests flip to drive the nt branch)."""
    return os.name == "nt"


def _suffix(real_data: Path) -> str:
    """Stable 8-hex per-install dir suffix (case-folded); 8 hex makes a collision negligible."""
    return f"{fnv1a_32(str(real_data.resolve()).lower().encode('utf-8')):08x}"


def _ensure_junction(link: Path, target: Path) -> None:
    """Create junction ``link`` -> ``target``, reusing ours; raise on a foreign-target collision.

    A dangling junction (target deleted) is cleared first; a junction pointing at a *different*
    live target (suffix collision) raises so the caller falls back rather than stealing it.
    """
    if link.exists():
        if os.path.realpath(link).lower() == os.path.realpath(target).lower():
            return
        msg = f"{link} already exists pointing elsewhere"
        raise OSError(msg)
    if os.path.lexists(link):
        _remove_link(link)
    _create_junction(link, target)


def _remove_link(link: Path) -> None:
    """Remove a junction (rmdir) or symlink (unlink) entry without touching its target."""
    if link.is_symlink():
        link.unlink()
    else:
        link.rmdir()


def _create_junction(link: Path, target: Path) -> None:
    """Create a directory junction ``link`` -> ``target`` via the native API (no shell)."""
    import _winapi  # noqa: PLC0415 — Windows-only; imported lazily so off-Windows never loads it

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]
