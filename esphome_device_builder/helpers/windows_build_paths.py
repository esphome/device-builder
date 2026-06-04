r"""
Relocate Windows build data to one short, space-free root (dodges MAX_PATH + spaces).

Native Windows ESP-IDF builds fail two ways from a normal config path: the 260-char ``MAX_PATH``
limit on the deep build tree, and a pioarduino whitespace guard / gcc ``-fdebug-prefix-map``
truncation when the path contains a space (common: ``C:\Users\First Last\...``).

:func:`windows_short_build_paths` points the build tree at ``C:\esphb-<id8>`` for the ``with``
block by setting ``ESPHOME_DATA_DIR`` = that root and ``PLATFORMIO_CORE_DIR`` = ``<root>\pio`` in
the process env (so ``CORE.data_dir`` and every compile subprocess resolve there). Existing
``<config>/.esphome`` and ``~/.platformio`` are moved in once (best-effort) so warm caches
survive. Real dirs (no junction), so CMake's REALPATH can't reintroduce the spaced/long path.
The root is left on uninstall (a reinstall keeps the warm toolchain); delete ``C:\esphb-*`` by
hand to reclaim space. No-op off Windows (including a Linux Docker container on Windows -- the
gate is ``os.name == "nt"``), and skipped if the user already set ``ESPHOME_DATA_DIR`` (a
deliberate path choice we don't override).
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from .dashboard_identity import get_or_create_dashboard_id

_LOGGER = logging.getLogger(__name__)

_ROOT_BASE = Path("C:\\")
_DASHBOARD_ID_CHARS = 8


@contextmanager
def windows_short_build_paths(config_dir: Path) -> Iterator[None]:
    """Point ESPHOME_DATA_DIR + PLATFORMIO_CORE_DIR at a short space-free root for the block."""
    if not _is_windows() or "ESPHOME_DATA_DIR" in os.environ:
        yield
        return

    root = _ROOT_BASE / f"esphb-{get_or_create_dashboard_id(config_dir)[:_DASHBOARD_ID_CHARS]}"
    pio = root / "pio"
    old_esphome = config_dir / ".esphome"
    if not root.exists():
        # First run: rename the existing build tree in (warm cache). The move creates the root,
        # so it precedes mkdir (else it would nest under it). Same-volume on the desktop target,
        # so it is an atomic os.rename.
        _try_move(old_esphome, root)
        if old_esphome.is_dir():
            # The move left the old tree in place (a cross-volume copy interrupted, or a
            # concurrent racer). Pointing env at the now-empty root would silently miss that
            # data, so stay on the original data dir; deep/spaced builds there may still fail.
            _LOGGER.warning(
                "Left Windows build data at %s; deep/spaced builds may fail", old_esphome
            )
            yield
            return
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        _LOGGER.exception("Could not create Windows build root; deep/spaced builds may fail")
        yield
        return
    # Toolchain move is best-effort and retried each run until it lands, so a crash mid-migration
    # self-heals; a failure here only re-downloads the toolchain, never a build-data read miss.
    if not pio.exists():
        _try_move(_platformio_dir(), pio)
    with suppress(OSError):
        pio.mkdir(parents=True, exist_ok=True)  # platformio recreates it if this fails

    prior_pio = os.environ.get("PLATFORMIO_CORE_DIR")
    os.environ["ESPHOME_DATA_DIR"] = str(root)
    os.environ["PLATFORMIO_CORE_DIR"] = str(pio)
    _LOGGER.info("Windows build data at %s (core %s)", root, pio)
    try:
        yield
    finally:
        # ESPHOME_DATA_DIR was unset on entry (guarded above), so popping is the right restore.
        os.environ.pop("ESPHOME_DATA_DIR", None)
        if prior_pio is None:
            os.environ.pop("PLATFORMIO_CORE_DIR", None)
        else:
            os.environ["PLATFORMIO_CORE_DIR"] = prior_pio


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    """Whether relocation applies (a seam tests flip to drive the nt branch)."""
    return os.name == "nt"


def _platformio_dir() -> Path:
    """Default toolchain dir to migrate from (a seam; tests avoid the real ~/.platformio)."""
    return Path.home() / ".platformio"


def _try_move(src: Path, dst: Path) -> None:
    """
    Move directory *src* to *dst* if it exists; log and continue on failure.

    A failed move never aborts relocation: that piece just rebuilds/re-downloads under the
    (now-empty) root, and the caller still points env at the root -- never a silent read miss.
    """
    if not src.is_dir():
        return
    try:
        shutil.move(str(src), str(dst))
    except OSError:
        _LOGGER.warning("Could not migrate %s to %s; it will be rebuilt", src, dst)
