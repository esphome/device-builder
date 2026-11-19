"""Measure the dashboard's disk usage, split into disjoint categories."""

from __future__ import annotations

import errno
import logging
import os
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from esphome.helpers import get_str_env

from ..models import DiskUsage
from .remote_build_layout import REMOTE_BUILDS_NAME, REMOTE_BUILDS_SUBDIR

_LOGGER = logging.getLogger(__name__)

# esphome's default for ``ESPHOME_BUILD_PATH``, relative to the data dir.
_DEFAULT_BUILD_PATH = "build"

# Depth of the toolchain freshness signature: a root, its packages, and their
# subdirs (``idf/frameworks`` moves when another ESP-IDF version lands).
_SIGNATURE_DEPTH = 2

# A path vanishing mid-walk (a build or cleanup running alongside) is not an error.
_GONE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR})


@dataclass
class _Walk:
    """State shared by one measurement's walks."""

    # (st_dev, st_ino) of multiply-linked files already counted.
    seen: set[tuple[int, int]] = field(default_factory=set)
    # Unreadable entries skipped, other than ones that vanished mid-walk.
    errors: int = 0


@dataclass
class ToolchainSizeCache:
    """Last toolchain total, reused while every root's freshness signature is unchanged."""

    signature: tuple[tuple[str, int], ...] = ()
    size: int = 0
    roots: tuple[Path, ...] = field(default_factory=tuple)


def build_roots(data_dir: Path, device_build_dirs: Iterable[Path]) -> list[Path]:
    """Return where builds live: the ``ESPHOME_BUILD_PATH`` root plus per-device ``build_path``s.

    Blocking (resolves paths); *data_dir* must already be resolved.
    """
    default = data_dir / get_str_env("ESPHOME_BUILD_PATH", _DEFAULT_BUILD_PATH)
    return _outermost(path.resolve() for path in (default, *device_build_dirs))


def measure_disk_usage(
    config_dir: Path,
    data_dir: Path,
    builds: list[Path],
    toolchain_cache: ToolchainSizeCache,
) -> DiskUsage:
    """Walk every category and read free space; blocking, run in an executor.

    *builds* comes from :func:`build_roots`. Each category's walk skips every
    other category's roots, so a tree nested inside another counts once.
    """
    config_dir, data_dir = config_dir.resolve(), data_dir.resolve()
    walk = _Walk()
    remote_builds = _outermost((data_dir / REMOTE_BUILDS_NAME, config_dir / REMOTE_BUILDS_SUBDIR))
    toolchains = _outermost(toolchain_dirs())
    every_root = (*builds, *remote_builds, *toolchains, data_dir, config_dir)

    def size(roots: Iterable[Path]) -> int:
        return sum(_tree_size(root, [r for r in every_root if r != root], walk) for root in roots)

    builds_bytes = size(builds)
    remote_builds_bytes = size(remote_builds)
    toolchains_bytes = _toolchains_size(toolchains, toolchain_cache, walk)
    cache_bytes = size((data_dir,))
    config_bytes = size((config_dir,))
    if walk.errors:
        _LOGGER.debug("Disk usage: skipped %d unreadable entries", walk.errors)
    total, free = read_volume_space((data_dir, *builds))
    return DiskUsage(
        total_bytes=total,
        free_bytes=free,
        builds_bytes=builds_bytes,
        remote_builds_bytes=remote_builds_bytes,
        toolchains_bytes=toolchains_bytes,
        cache_bytes=cache_bytes,
        config_bytes=config_bytes,
        measured_at=time.time(),
    )


def refresh_volume_space(usage: DiskUsage, paths: Iterable[Path]) -> DiskUsage:
    """Return *usage* with fresh volume totals; the category sizes are kept."""
    total, free = read_volume_space(paths)
    return replace(usage, total_bytes=total, free_bytes=free)


def read_volume_space(paths: Iterable[Path]) -> tuple[int, int]:
    """Return ``(total, free)`` bytes of the fullest volume holding any of *paths*.

    A separately mounted build volume (``ESPHOME_BUILD_PATH=/build``) filling
    up fails compiles just like the data volume, so the lower free space wins.
    """
    volumes = [space for path in paths if (space := _volume_space(path)) is not None]
    return min(volumes, key=lambda space: space[1], default=(0, 0))


def _volume_space(path: Path) -> tuple[int, int] | None:
    # Nearest existing ancestor: the data and build dirs only appear on the first compile.
    for candidate in (path, *path.absolute().parents):
        try:
            usage = shutil.disk_usage(candidate)
        except OSError:
            continue
        return usage.total, usage.free
    return None


def toolchain_dirs() -> list[Path]:
    """Return the existing toolchain dirs ``esphome clean-all`` wipes besides the data dir."""
    # Deferred: platformdirs / platformio / esphome's tools cache are only needed here.
    import platformdirs  # noqa: PLC0415
    from esphome.build_helpers.tools_cache import (  # noqa: PLC0415
        TOOLS_CACHE_SPECS,
        tools_cache_path,
    )
    from esphome.platformio.toolchain import get_platformio_config  # noqa: PLC0415

    dirs = [Path(platformdirs.user_cache_dir("esphome", appauthor=False))]
    dirs.extend(tools_cache_path(*spec) for spec in TOOLS_CACHE_SPECS)
    if (config := get_platformio_config()) is not None:
        options = ("core_dir", "cache_dir", "packages_dir", "platforms_dir")
        dirs.extend(Path(config.get("platformio", option)) for option in options)
    return [d.resolve() for d in dirs if d.is_dir()]


def _toolchains_size(roots: list[Path], cache: ToolchainSizeCache, walk: _Walk) -> int:
    signature = _signature(roots)
    if signature == cache.signature and tuple(roots) == cache.roots:
        # Unwalked roots stay out of the later categories via their skip lists.
        return cache.size
    errors_before = walk.errors
    size = sum(_tree_size(root, (), walk) for root in roots)
    # An unreadable entry makes the total short; walk again next time rather than keep it.
    if walk.errors == errors_before:
        cache.signature, cache.size, cache.roots = signature, size, tuple(roots)
    else:
        cache.signature, cache.roots = (), ()
    return size


def _signature(roots: Iterable[Path]) -> tuple[tuple[str, int], ...]:
    """Return mtimes of each root and its subdirs down to ``_SIGNATURE_DEPTH``.

    Adding or removing a package (or a framework version inside one) moves it.
    """
    entries: list[tuple[str, int]] = []
    level = [str(root) for root in roots]
    entries.extend((path, _mtime_ns(Path(path))) for path in level)
    for _ in range(_SIGNATURE_DEPTH):
        below: list[str] = []
        for parent in level:
            try:
                it = os.scandir(parent)
            except OSError:
                continue
            with it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            mtime = entry.stat(follow_symlinks=False).st_mtime_ns
                            entries.append((entry.path, mtime))
                            below.append(entry.path)
                    except OSError:
                        continue
        level = below
    return tuple(sorted(entries))


def _mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _outermost(paths: Iterable[Path]) -> list[Path]:
    """Drop paths nested inside another path in *paths* (and duplicates)."""
    unique = sorted(set(paths), key=lambda p: len(p.parts))
    kept: list[Path] = []
    for path in unique:
        if not any(path.is_relative_to(parent) for parent in kept):
            kept.append(path)
    return kept


def _tree_size(root: Path, skip: Iterable[Path], walk: _Walk) -> int:
    """Sum regular-file sizes under *root*, not descending into *skip* or symlinks.

    Unreadable entries are skipped and counted in ``walk.errors`` rather than
    failing the pass: one root-owned folder shouldn't blank the whole report.
    """
    skip_set = {str(p) for p in skip}
    total = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as err:
            _note_error(walk, err)
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.path not in skip_set:
                            stack.append(entry.path)
                        continue
                    if entry.is_file(follow_symlinks=False):
                        total += _file_size(entry, walk)
                except OSError as err:
                    _note_error(walk, err)
    return total


def _file_size(entry: os.DirEntry[str], walk: _Walk) -> int:
    """Return *entry*'s size, or 0 for a further link to a file already counted."""
    stat = entry.stat(follow_symlinks=False)
    if stat.st_ino == 0:
        # Windows' DirEntry.stat zeroes st_ino/st_dev/st_nlink; a real stat fills them.
        stat = Path(entry.path).stat(follow_symlinks=False)
    if stat.st_nlink > 1:
        key = (stat.st_dev, stat.st_ino)
        if key in walk.seen:
            return 0
        walk.seen.add(key)
    return stat.st_size


def _note_error(walk: _Walk, err: OSError) -> None:
    if err.errno not in _GONE_ERRNOS:
        walk.errors += 1
