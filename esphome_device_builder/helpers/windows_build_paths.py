r"""
Relocate Windows build data to one short, space-free root (dodges MAX_PATH + spaces).

Native Windows ESP-IDF builds fail two ways from a normal config path: the 260-char ``MAX_PATH``
limit on the deep build tree and toolchain install (the native ESP-IDF toolchain nests ~209 chars
below its install dir), and spaces in the path (common: ``C:\Users\First Last\...``) — ESP-IDF
refuses spaced install paths, and pioarduino has its own whitespace guard / gcc
``-fdebug-prefix-map`` truncation.

:func:`windows_short_build_paths` points the build tree at ``C:\esphb\<id8>`` for the ``with``
block by setting ``ESPHOME_DATA_DIR`` = that root, ``PLATFORMIO_CORE_DIR`` = ``<root>\pio`` and
``ESPHOME_ESP_IDF_PREFIX`` = the shared ``C:\esphb\idf`` in the process env (so ``CORE.data_dir``
and every compile subprocess resolve there, whichever toolchain the config selects).
Per-dashboard roots nest under one ``C:\esphb`` parent rather than scattering ``C:\esphb-*``
across the drive root. The IDF toolchain alone is shared across dashboards, beside the roots:
gcc probes its multilib include dirs via un-normalized ``bin/../lib/...`` self-relative paths
that reach ~245 characters below the IDF tools dir (esphome/esphome#16896), so the prefix must
stay within 15 characters of ``MAX_PATH`` -- ``C:\esphb\<id8>\idf`` (21) overflows, ``C:\esphb\idf``
(12) fits, and sharing matches upstream's machine-global cache semantics (version-safe: the
tools dir is per-version inside). Existing data is moved in once (best-effort) so warm caches
survive: from the legacy flat ``C:\esphb-<id8>`` of the first relocation release, else from
``<config>/.esphome`` and ``~/.platformio``. The shared IDF dir is the exception: nothing is
ever migrated into it and the toolchain installs fresh -- penv paths bake into every configured
build dir, so a *moved* IDF tree fails idf.py's active-vs-configured python check -- and an
earlier release's per-dashboard ``<root>\idf`` is deleted rather than swept in. esphome's
machine-global IDF cache is left untouched for native CLI use.
Real dirs (no junction), so CMake's REALPATH can't reintroduce the spaced/long path. The tree
is left on uninstall (a reinstall keeps the warm toolchain); delete ``C:\esphb`` by hand to
reclaim space. No-op off Windows (including a
Linux Docker container on Windows -- the gate is ``os.name == "nt"``), and skipped if the user
already set ``ESPHOME_DATA_DIR`` (a deliberate path choice we don't override).
"""

from __future__ import annotations

import logging
import os
import shutil
import string
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from esphome.helpers import rmtree

from .dashboard_identity import get_or_create_dashboard_id

_LOGGER = logging.getLogger(__name__)

_ROOT_BASE = Path("C:\\esphb")
# One name ties the shared toolchain dir, the per-dashboard purge target, and the
# reserved-suffix guard together; a rename can't silently reopen the collision.
_IDF_DIRNAME = "idf"
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
    """Point the build-data + toolchain env vars at a short space-free root for the block."""
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
    if not suffix or suffix.lower() == _IDF_DIRNAME:
        # Hand-corrupted sidecar only: empty would collapse the root onto the shared C:\esphb
        # parent; "idf" would collide with the shared toolchain dir beside the roots.
        _LOGGER.warning("dashboard_id sanitized to %r; skipping build relocation", suffix)
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

    saved: dict[str, str | None] = {"ESPHOME_DATA_DIR": None}  # absent on entry (guarded above)
    os.environ["ESPHOME_DATA_DIR"] = str(root)
    # Relocate each toolchain unless the user deliberately set its env var (leave their choice
    # and their existing install untouched), or a corrupt partial copy can't be made clean.
    override_pio = "PLATFORMIO_CORE_DIR" not in os.environ and _relocate_into(
        pio, _platformio_dir()
    )
    if override_pio:
        saved["PLATFORMIO_CORE_DIR"] = None  # only overridden when absent
        os.environ["PLATFORMIO_CORE_DIR"] = str(pio)
    # Unlike ESPHOME_DATA_DIR (bare presence wins in CORE.data_dir), esphome treats an
    # empty/whitespace ESPHOME_ESP_IDF_PREFIX as unset — mirror that or an empty var would
    # silently fall back to the long + spaced machine-global cache this exists to avoid.
    prev_idf = os.environ.get("ESPHOME_ESP_IDF_PREFIX")
    user_set_idf = bool(prev_idf and prev_idf.strip())
    idf_dir = None if user_set_idf else _resolve_idf_dir(root / _IDF_DIRNAME)
    if idf_dir is not None:
        saved["ESPHOME_ESP_IDF_PREFIX"] = prev_idf  # may be present-but-empty; kept verbatim
        os.environ["ESPHOME_ESP_IDF_PREFIX"] = str(idf_dir)
    _LOGGER.info(
        "Windows build data at %s (pio %s, idf %s)",
        root,
        pio if override_pio else "default",
        idf_dir or "default",
    )
    try:
        yield
    finally:
        _restore_env(saved)


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


def _restore_env(saved: dict[str, str | None]) -> None:
    """Put each overridden env var back exactly as found (``None`` means absent)."""
    for var, prev in saved.items():
        if prev is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = prev


def _resolve_idf_dir(dashboard_idf: Path) -> Path | None:
    r"""
    Return the ESP-IDF install dir to point ``ESPHOME_ESP_IDF_PREFIX`` at, or ``None``.

    Shared across dashboards: the prefix must stay within 15 chars of ``MAX_PATH`` (module
    docstring), which a per-dashboard ``C:\esphb\<id8>\idf`` (21) cannot. The shared dir is
    never seeded by moving an existing IDF tree — penv paths bake into every configured build
    dir, so a moved tree fails idf.py's active-vs-configured python check — the toolchain
    installs fresh, and an earlier release's *dashboard_idf* is deleted.
    """
    idf = _ROOT_BASE / _IDF_DIRNAME
    if not _finalize_dir(idf):
        # Unrelocated, esphome falls back to its machine-global cache — the long + spaced path
        # this exists to avoid — so name it before the compile fails cryptically.
        _LOGGER.warning(
            "ESP-IDF toolchain not relocated; native builds will use %s, where deep or spaced "
            "paths may fail",
            _default_idf_cache() or "esphome's default cache location",
        )
        return None
    if dashboard_idf.is_dir():
        try:
            rmtree(dashboard_idf)
        except OSError as err:
            _LOGGER.warning(
                "Could not remove stale per-dashboard ESP-IDF %s: %s", dashboard_idf, err
            )
    return idf


def _default_idf_cache() -> Path | None:
    """Esphome's machine-global native-IDF install dir, named in the not-relocated warning."""
    # platformdirs ships with the esphome extra, not this package.
    try:
        import platformdirs  # noqa: PLC0415
    except ImportError:
        _LOGGER.debug("platformdirs unavailable; cannot name the default IDF cache")
        return None
    return Path(platformdirs.user_cache_dir("esphome", appauthor=False)) / "idf"


def _relocate_into(dst: Path, *sources: Path | None) -> bool:
    """
    Move the first existing of *sources* into *dst* once; return whether *dst* is trusted.

    *sources* are tried in priority order (e.g. the legacy flat root, then the original
    ``.esphome``); the first that exists is the authoritative copy and is moved in. A ``.json``
    completion marker under *dst* (preserved by esphome clean / clean-all) records a finished move;
    it is keyed off a source still existing, not bare ``dst.exists()``, so a marker write lost
    after a successful move never triggers a destructive re-relocation. Returns ``False`` when the
    move is incomplete -- a partial *dst* that could not be cleared, or a move that left the source
    behind -- so the caller never points env at incomplete data or a corrupt toolchain. Used for
    both the build root and the toolchains so the paths cannot drift apart.
    """
    marker = dst / _RELOCATED_MARKER
    if marker.is_file():
        return True  # already relocated; trust dst, ignore any stale leftover at the source
    src = next((s for s in sources if s is not None and s.is_dir()), None)
    if src is not None:
        # Source still present, so the move never completed. A partial dst from an interrupted
        # cross-volume copy would nest the retry, so discard it before re-moving.
        if dst.exists():
            try:
                rmtree(dst)  # esphome's rmtree clears the read-only flags toolchain trees carry
            except OSError as err:
                _LOGGER.warning(
                    "Could not clear partial %s (%s); leaving %s in place", dst, err, src
                )
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
    return _finalize_dir(dst)


def _finalize_dir(dst: Path) -> bool:
    """
    Ensure *dst* exists and bears the completion marker; return whether it is trusted.

    The terminal step of :func:`_relocate_into`, also used alone for a dir nothing is
    ever moved into (the shared IDF install target).
    """
    marker = dst / _RELOCATED_MARKER
    if marker.is_file():
        return True
    try:
        dst.mkdir(parents=True, exist_ok=True)
        marker.write_text("{}", encoding="utf-8")
    except OSError as err:
        _LOGGER.warning("Could not finalize relocation dir %s: %s", dst, err)
        return False
    return True
