r"""
Relocate Windows build data to one short, space-free root (dodges MAX_PATH + spaces).

Native Windows ESP-IDF builds fail two ways from a normal config path: the 260-char ``MAX_PATH``
limit on the deep build tree, and a pioarduino whitespace guard / gcc ``-fdebug-prefix-map``
truncation when the path contains a space (common: ``C:\Users\First Last\...``).

:func:`windows_short_build_paths` points the build tree at ``C:\esphb\<id8>`` for the ``with``
block by setting ``ESPHOME_DATA_DIR`` = that root and ``PLATFORMIO_CORE_DIR`` = ``<root>\pio`` in
the process env (so ``CORE.data_dir`` and every compile subprocess resolve there). Per-dashboard
roots nest under one ``C:\esphb`` parent rather than scattering ``C:\esphb-*`` across the drive
root. Existing data is moved in once (best-effort) so warm caches survive: from the legacy flat
``C:\esphb-<id8>`` of the first relocation release, else from ``<config>/.esphome`` +
``~/.platformio``. Real dirs (no junction), so CMake's REALPATH can't reintroduce the spaced/long
path. The tree is left on uninstall (a reinstall keeps the warm toolchain); delete ``C:\esphb`` by
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

_ROOT_BASE = Path("C:\\esphb")
# Legacy flat base from the first relocation release (``C:\esphb-<id8>``); migrated in once.
_LEGACY_ROOT_BASE = Path("C:\\")
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
    suffix = _safe_suffix(dashboard_id)
    if not suffix:
        # A hand-corrupted sidecar whose chars are all stripped would collapse the root onto the
        # shared C:\esphb parent; refuse rather than nest every dashboard inside one another.
        _LOGGER.warning("dashboard_id sanitized to empty; skipping build relocation")
        yield
        return
    new_root = _ROOT_BASE / suffix  # C:\esphb\<id8>
    legacy_root = _LEGACY_ROOT_BASE / f"esphb-{suffix}"  # C:\esphb-<id8>, first-relocation layout
    if _relocate_into(new_root, legacy_root, config_dir / ".esphome"):
        root = new_root
    elif (legacy_root / _RELOCATED_MARKER).is_file():
        # Could not move to the tidy nested location, but the first-relocation data is intact;
        # keep using it this session (a migrated config_dir/.esphome is empty, so no-op would miss).
        _LOGGER.warning("Using legacy build root %s; could not move to %s", legacy_root, new_root)
        root = legacy_root
    else:
        yield
        return
    pio = root / "pio"

    os.environ["ESPHOME_DATA_DIR"] = str(root)
    # Relocate the toolchain unless the user deliberately set PLATFORMIO_CORE_DIR (leave their
    # choice and their ~/.platformio untouched), or a corrupt partial copy can't be made clean.
    user_set_pio = "PLATFORMIO_CORE_DIR" in os.environ
    override_pio = not user_set_pio and _relocate_into(pio, _platformio_dir())
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


def _relocate_into(dst: Path, *sources: Path) -> bool:
    """
    Move the first existing of *sources* into *dst* once; return whether *dst* is trusted.

    *sources* are tried in priority order (e.g. the legacy flat root, then the original
    ``.esphome``); the first that exists is the authoritative copy and is moved in. A ``.json``
    completion marker under *dst* (preserved by esphome clean / clean-all) records a finished move;
    it is keyed off a source still existing, not bare ``dst.exists()``, so a marker write lost
    after a successful move never triggers a destructive re-relocation. Returns ``False`` when the
    move is incomplete -- a partial *dst* that could not be cleared, or a move that left the source
    behind -- so the caller never points env at incomplete data or a corrupt toolchain. Used for
    both the build root and the toolchain so the two paths cannot drift apart.
    """
    marker = dst / _RELOCATED_MARKER
    if marker.is_file():
        return True  # already relocated; trust dst, ignore any stale leftover at the source
    src = next((s for s in sources if s.is_dir()), None)
    if src is not None:
        # Source still present, so the move never completed. A partial dst from an interrupted
        # cross-volume copy would nest the retry, so discard it before re-moving.
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
            if dst.exists():
                _LOGGER.warning("Could not clear partial %s; leaving %s in place", dst, src)
                return False
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)  # nested rename target needs its parent
            shutil.move(str(src), str(dst))
        except OSError:
            _LOGGER.warning("Could not move %s to %s; it will be rebuilt", src, dst)
        if src.is_dir():
            _LOGGER.warning("%s not relocated; source remains at %s", dst, src)
            return False
    # No source left here: none existed, the move just completed, or a prior run moved it and only
    # the marker write was lost. dst is authoritative either way.
    try:
        dst.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
    except OSError:
        _LOGGER.warning("Could not finalize relocation dir %s", dst)
        return False
    return True
