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
import string
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .dashboard_identity import get_or_create_dashboard_id

_LOGGER = logging.getLogger(__name__)

_ROOT_BASE = Path("C:\\")
_DASHBOARD_ID_CHARS = 8
# dashboard_id is base64url (token_urlsafe), so this is a no-op in practice; it guards a hand
# -corrupted sidecar from injecting path separators / a drive prefix into the root segment.
_SAFE_SUFFIX_CHARS = frozenset(string.ascii_letters + string.digits + "_-")
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
    root = _ROOT_BASE / f"esphb-{_safe_suffix(dashboard_id)}"
    pio = root / "pio"
    if not _relocate_into(config_dir / ".esphome", root):
        yield
        return

    os.environ["ESPHOME_DATA_DIR"] = str(root)
    # Relocate the toolchain unless the user deliberately set PLATFORMIO_CORE_DIR (leave their
    # choice and their ~/.platformio untouched), or a corrupt partial copy can't be made clean.
    user_set_pio = "PLATFORMIO_CORE_DIR" in os.environ
    override_pio = not user_set_pio and _relocate_into(_platformio_dir(), pio)
    if override_pio:
        os.environ["PLATFORMIO_CORE_DIR"] = str(pio)
    _LOGGER.info("Windows build data at %s (core %s)", root, pio if override_pio else "default")
    try:
        yield
    finally:
        # Both vars were unset on entry (ESPHOME_DATA_DIR guarded above; PLATFORMIO_CORE_DIR only
        # overridden when it was absent), so popping is the right restore.
        os.environ.pop("ESPHOME_DATA_DIR", None)
        if override_pio:
            os.environ.pop("PLATFORMIO_CORE_DIR", None)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _safe_suffix(dashboard_id: str) -> str:
    """First 8 filename-safe chars of *dashboard_id*; stable across runs for the same id."""
    return "".join(c for c in dashboard_id if c in _SAFE_SUFFIX_CHARS)[:_DASHBOARD_ID_CHARS]


def _is_windows() -> bool:
    """Whether relocation applies (a seam tests flip to drive the nt branch)."""
    return os.name == "nt"


def _platformio_dir() -> Path:
    """Default toolchain dir to migrate from (a seam; tests avoid the real ~/.platformio)."""
    return Path.home() / ".platformio"


def _relocate_into(src: Path, dst: Path) -> bool:
    """
    Move directory *src* into *dst* once; return whether *dst* is a complete, trusted relocation.

    A ``.json`` completion marker under *dst* (preserved by esphome clean / clean-all) records a
    finished move; it is keyed off *src* still existing, not bare ``dst.exists()``, so a marker
    write lost after a successful move never triggers a destructive re-relocation. Returns
    ``False`` when the move is incomplete -- a partial *dst* from an interrupted cross-volume copy
    that could not be cleared, or a move that left *src* behind -- so the caller never points env
    at incomplete data or a corrupt toolchain. Used for both the build root and the toolchain so
    the two paths cannot drift apart.
    """
    marker = dst / _RELOCATED_MARKER
    if marker.is_file():
        return True  # already relocated; trust dst, ignore any stale leftover at the source
    if src.is_dir():
        # Source still present, so the move never completed. A partial dst from an interrupted
        # cross-volume copy would nest the retry, so discard it before re-moving.
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
            if dst.exists():
                _LOGGER.warning("Could not clear partial %s; leaving %s in place", dst, src)
                return False
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            _LOGGER.warning("Could not move %s to %s; it will be rebuilt", src, dst)
        if src.is_dir():
            _LOGGER.warning("%s not relocated; source remains at %s", dst, src)
            return False
    # src gone here: it never existed, the move just completed, or a prior run moved it and only
    # the marker write was lost. dst is authoritative either way.
    try:
        dst.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
    except OSError:
        _LOGGER.warning("Could not finalize relocation dir %s", dst)
        return False
    return True
