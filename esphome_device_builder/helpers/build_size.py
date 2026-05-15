"""
Cached, mtime-gated walk of a device's per-build artifact directory.

ESPHome compiles produce a meaty ``.esphome/build/<device>/``
tree (50-250 MB typical, multi-gigabyte for heavy configs). The
dashboard surfaces this size in the per-device drawer; the walk
itself is heavy I/O. We cache the computed total in the
per-device metadata sidecar keyed off a *freshness pair*:
``(dir_mtime, build_info_mtime)``. Either side moving counts
as stale.

Both halves are needed — empirical matrix (Python 3.12,
macOS APFS) for the write patterns ESPHome / PlatformIO use::

    case                      | dir       | firmware  | sibling
    --------------------------+-----------+-----------+----------
    truncate + same content   | unchanged | moved     | unchanged
    truncate + diff content   | unchanged | moved     | unchanged
    atomic-replace (rename)   | MOVED     | moved     | unchanged
    add a sibling             | MOVED     | unchanged | unchanged
    remove a sibling          | MOVED     | unchanged | unchanged
    write_file_if_changed=    | unchanged | unchanged | unchanged
    write_file_if_changed≠    | unchanged | unchanged | MOVED

``write_file_if_changed`` (ESPHome's writer) skips identical
content, so a no-op recompile is a no-op for the cache too.
When the compile produces different output, ``build_info.json``
itself moves (caught by ``build_info_mtime``) but the parent
dir's mtime doesn't (PlatformIO sibling churn is what moves
that — caught by ``dir_mtime``).

The build path comes from each device's ``StorageJSON``
sidecar via :func:`resolve_build_dir`. Devices that haven't
been compiled return ``None`` — callers treat that as "no
cached value, total = 0".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from esphome.storage_json import StorageJSON

from .storage_path import resolve_storage_path

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BuildDirSignal:
    """Two-stat freshness signal for a per-device build directory.

    ``dir_mtime`` and ``info_mtime`` are whole-second
    ``int(stat.st_mtime)`` values; ``0`` means that path is
    missing. The cache treats inequality on either half as
    stale (see the module docstring's empirical matrix).
    """

    dir_mtime: int
    info_mtime: int


@dataclass(frozen=True, slots=True)
class BuildSizeRefreshResult:
    """The persisted triple after a successful walk: size + freshness pair."""

    size_bytes: int
    signal: BuildDirSignal


def resolve_build_dir(filename: str) -> Path | None:
    """
    Return the per-device build directory path, or ``None`` if unknown.

    Reads ``build_path`` off the device's StorageJSON sidecar.
    ``None`` covers both "no StorageJSON" (never compiled) and
    "older StorageJSON with no build_path" — both legitimately
    mean "no build artifacts to size."
    """
    try:
        storage = StorageJSON.load(resolve_storage_path(filename))
    except Exception:
        # StorageJSON.load returns None on missing-file but
        # raises on a corrupt one; treat both as "no artifacts."
        _LOGGER.debug("StorageJSON load failed for %s", filename, exc_info=True)
        return None
    if storage is None or not storage.build_path:
        return None
    return Path(storage.build_path)


def get_build_dir_signal(build_dir: Path) -> BuildDirSignal:
    """Return the :class:`BuildDirSignal` freshness pair for *build_dir*."""
    return BuildDirSignal(
        dir_mtime=get_build_dir_mtime(build_dir),
        info_mtime=get_build_dir_mtime(build_dir / "build_info.json"),
    )


def find_stale_build_dirs(
    filenames: list[str],
    metadata: dict[str, dict[str, object]],
) -> list[str]:
    """
    Cheap fleet-wide stat: return the subset whose freshness pair moved.

    Caller passes a snapshot of the per-device metadata store
    (one RAM dict for the whole fleet). Returns the filenames
    where current ≠ cached, in the caller's input order —
    including the ``current = (0, 0)`` + cached non-zero case
    (build dir vanished after the last walk and the cache still
    needs clearing). Devices with no StorageJSON / no
    ``build_path`` are skipped silently.
    """
    stale: list[str] = []
    for filename in filenames:
        build_dir = resolve_build_dir(filename)
        if build_dir is None:
            continue
        current = get_build_dir_signal(build_dir)
        entry = metadata.get(filename, {})
        cached = BuildDirSignal(
            dir_mtime=coerce_sidecar_int(entry.get("build_size_dir_mtime")),
            info_mtime=coerce_sidecar_int(entry.get("build_size_info_mtime")),
        )
        # Equality covers every case: dir-gone + cache-empty
        # short-circuits, dir-gone + cache-populated falls
        # through to walk (which clears), dir-present +
        # cache-stale falls through to walk. Don't
        # short-circuit on "current empty" — it leaked stale
        # non-zero cache state when a user manually wiped a
        # build dir.
        if current != cached:
            stale.append(filename)
    return stale


def refresh_build_size_if_stale(
    filename: str,
    cached_signal: BuildDirSignal,
) -> BuildSizeRefreshResult | None:
    """
    Stat + walk the per-device build dir when its freshness pair moved.

    Pure I/O — returns the new size + signal on a miss, ``None``
    on cache-hit. Caller persists the result through the device
    metadata store (the store's debounced write happens on the
    event loop after this executor unit returns).

    Short-circuits the "build dir missing AND cache empty"
    case (``current == (0, 0) == cached``) so the steady-state
    poll path doesn't return on every missing build.
    """
    build_dir = resolve_build_dir(filename)
    if build_dir is None:
        return None
    current = get_build_dir_signal(build_dir)
    if current == cached_signal:
        return None
    size = compute_build_dir_size(build_dir)
    return BuildSizeRefreshResult(size_bytes=size, signal=current)


def coerce_sidecar_int(value: object) -> int:
    """Coerce a cached sidecar value to ``int``; ``0`` on any failure.

    A hand-edited or partially-written sidecar can land here
    with a non-numeric value; falling back to ``0`` matches the
    "never walked" sentinel and the next refresh repopulates.
    """
    # Narrow before ``int(...)`` so mypy can resolve the
    # overload — lists / dicts fall straight to the sentinel
    # without an exception round-trip.
    if not isinstance(value, (int, float, str, bytes)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_build_dir_mtime(path: Path) -> int:
    """
    Stat *path*'s mtime in whole seconds. ``0`` when missing.

    Truncated to whole seconds (not the float ``st_mtime``)
    for cross-filesystem safety: FAT32 / older NFS / CIFS round
    to the nearest second on write, so a cached fractional
    value from one filesystem would never compare equal to the
    truncated re-stat after a cross-mount move and the
    dashboard would walk the tree on every poll. Cost is one
    false-negative re-walk per second of clock drift around a
    real change.
    """
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def compute_build_dir_size(build_dir: Path) -> int:
    """
    Sum every regular-file size under *build_dir*, recursive.

    Heavy + I/O-bound — caller is responsible for gating
    behind the mtime check and for running off the event loop.
    Returns ``0`` when the dir is missing or empty. Per-entry
    ``OSError`` (vanishing files during a concurrent compile)
    is swallowed so the total reflects what we could see.

    Walks via :func:`os.walk` rather than :meth:`Path.rglob` —
    ``os.walk`` delegates to :func:`os.scandir` which carries
    cached ``d_type`` from ``readdir()``, halving the syscall
    count vs ``rglob`` on 10k-file PlatformIO trees.
    """
    total = 0
    # ``onerror`` swallows top-level failures (missing dir,
    # permission denied at root). Per-file errors are caught
    # below.
    for dirpath, _dirnames, filenames in os.walk(build_dir, onerror=lambda _e: None):
        dir_path = Path(dirpath)
        for filename in filenames:
            try:
                total += (dir_path / filename).stat().st_size
            except OSError:
                continue
    return total
