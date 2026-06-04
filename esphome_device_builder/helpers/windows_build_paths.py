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
# Written into the root only after the build-data move fully completes; ``.json`` so esphome's
# clean / clean-all preserve it. Distinguishes a finished relocation from a partial one, so a
# later stale write to the old location isn't mistaken for unfinished work.
_RELOCATED_MARKER = ".device-builder-relocated.json"


@contextmanager
def windows_short_build_paths(config_dir: Path) -> Iterator[None]:
    """Point ESPHOME_DATA_DIR + PLATFORMIO_CORE_DIR at a short space-free root for the block."""
    if not _is_windows() or "ESPHOME_DATA_DIR" in os.environ:
        yield
        return

    try:
        dashboard_id = get_or_create_dashboard_id(config_dir)
    except OSError:
        _LOGGER.exception("Could not resolve dashboard_id; deep/spaced builds may fail")
        yield
        return
    root = _ROOT_BASE / f"esphb-{dashboard_id[:_DASHBOARD_ID_CHARS]}"
    pio = root / "pio"
    if not _relocate_data(config_dir, root):
        yield
        return

    prior_pio = os.environ.get("PLATFORMIO_CORE_DIR")
    os.environ["ESPHOME_DATA_DIR"] = str(root)
    use_pio = _relocate_toolchain(pio)
    if use_pio:
        os.environ["PLATFORMIO_CORE_DIR"] = str(pio)
    _LOGGER.info("Windows build data at %s (core %s)", root, pio if use_pio else "default")
    try:
        yield
    finally:
        # ESPHOME_DATA_DIR was unset on entry (guarded above), so popping is the right restore.
        os.environ.pop("ESPHOME_DATA_DIR", None)
        if use_pio:
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


def _relocate_data(config_dir: Path, root: Path) -> bool:
    """
    Move ``<config>/.esphome`` into *root* once; return whether *root* is safe as the data dir.

    Returns ``False`` (caller stays on the original data dir, retries next run) when relocation is
    incomplete: a partial *root* from an interrupted cross-volume move that we could not clear, or
    a move that left the source behind. The completion marker is keyed off the *source* still
    existing, not mere ``root.exists()``, so a marker write that crashed after a successful move
    does not trigger a destructive re-relocation.
    """
    marker = root / _RELOCATED_MARKER
    if marker.is_file():
        return True  # already relocated; trust root, ignore any stale leftover at the old location
    old_esphome = config_dir / ".esphome"
    if old_esphome.is_dir():
        # Source still present, so the move never completed. A partial root from an interrupted
        # cross-volume copy would nest the retry, so discard it before re-moving.
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
            if root.exists():
                _LOGGER.warning(
                    "Could not clear partial build root %s; staying on %s", root, old_esphome
                )
                return False
        _try_move(old_esphome, root)
        if old_esphome.is_dir():
            _LOGGER.warning(
                "Left Windows build data at %s; deep/spaced builds may fail", old_esphome
            )
            return False
    # old_esphome gone here: it never existed (fresh), or the move just completed, or a prior run
    # moved it and only the marker write was lost. root is authoritative either way.
    try:
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
    except OSError:
        _LOGGER.exception("Could not create Windows build root; deep/spaced builds may fail")
        return False
    return True


def _relocate_toolchain(pio: Path) -> bool:
    """
    Move ~/.platformio into *pio* once; return whether *pio* is safe as PLATFORMIO_CORE_DIR.

    Best-effort and retried each run while *pio* is absent, so a crash before the toolchain lands
    self-heals. Returns ``False`` (caller leaves PLATFORMIO_CORE_DIR untouched, so platformio uses
    its default toolchain) only when an interrupted move left a half-copied *pio* we could not
    discard -- a long default path beats building against a corrupt toolchain.
    """
    old_pio = _platformio_dir()
    if not pio.exists() and old_pio.is_dir():
        _try_move(old_pio, pio)
        if old_pio.is_dir():
            _LOGGER.warning("Toolchain move from %s incomplete; discarding partial copy", old_pio)
            shutil.rmtree(pio, ignore_errors=True)
            if pio.exists() and any(pio.iterdir()):
                _LOGGER.warning("Could not discard partial toolchain at %s; using default", pio)
                return False
    with suppress(OSError):
        pio.mkdir(parents=True, exist_ok=True)  # platformio recreates it if this fails
    return True


def _try_move(src: Path, dst: Path) -> None:
    """
    Move directory *src* to *dst* if it exists; log and continue on failure.

    A failed move never aborts relocation: the caller's guards decide what to do, so this never
    leaves env pointed at incomplete data.
    """
    if not src.is_dir():
        return
    try:
        shutil.move(str(src), str(dst))
    except OSError:
        _LOGGER.warning("Could not migrate %s to %s; it will be rebuilt", src, dst)
