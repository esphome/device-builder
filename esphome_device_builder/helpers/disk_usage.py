"""Measure the dashboard's disk usage, split into disjoint categories."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..models import DiskUsage
from .remote_build_layout import REMOTE_BUILDS_NAME

_BUILD_DIR_NAME = "build"

# (st_dev, st_ino) pairs already counted; keeps hardlinks and overlapping roots single.
type _Seen = set[tuple[int, int]]


@dataclass
class ToolchainSizeCache:
    """Last toolchain total, reused while every root's freshness signature is unchanged."""

    signature: tuple[tuple[str, int], ...] = ()
    size: int = 0
    roots: tuple[Path, ...] = field(default_factory=tuple)


def measure_disk_usage(
    config_dir: Path, data_dir: Path, toolchain_cache: ToolchainSizeCache
) -> DiskUsage:
    """Walk every category and read the volume's free space; blocking, run in an executor."""
    config_dir, data_dir = config_dir.resolve(), data_dir.resolve()
    seen: _Seen = set()
    builds = data_dir / _BUILD_DIR_NAME
    remote_builds = data_dir / REMOTE_BUILDS_NAME
    toolchain_roots = _outermost(toolchain_dirs())
    builds_bytes = _tree_size(builds, (), seen)
    remote_builds_bytes = _tree_size(remote_builds, (), seen)
    toolchains_bytes = _toolchains_size(toolchain_roots, toolchain_cache, seen)
    cache_bytes = _tree_size(data_dir, (builds, remote_builds, *toolchain_roots), seen)
    config_bytes = _tree_size(config_dir, (data_dir, *toolchain_roots), seen)
    total, free = read_volume_space(data_dir)
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


def refresh_volume_space(usage: DiskUsage, data_dir: Path) -> DiskUsage:
    """Return *usage* with fresh volume totals; the category sizes are kept."""
    total, free = read_volume_space(data_dir)
    return replace(usage, total_bytes=total, free_bytes=free)


def read_volume_space(data_dir: Path) -> tuple[int, int]:
    """Return ``(total, free)`` bytes for the volume *data_dir* lives (or will live) on."""
    # Nearest existing ancestor: the data dir only appears on the first compile.
    for candidate in (data_dir, *data_dir.absolute().parents):
        try:
            usage = shutil.disk_usage(candidate)
        except OSError:
            continue
        return usage.total, usage.free
    return 0, 0


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


def _toolchains_size(roots: list[Path], cache: ToolchainSizeCache, seen: _Seen) -> int:
    signature = _signature(roots)
    if signature == cache.signature and tuple(roots) == cache.roots:
        # Unwalked roots stay out of the later categories via their skip lists.
        return cache.size
    size = sum(_tree_size(root, (), seen) for root in roots)
    cache.signature, cache.size, cache.roots = signature, size, tuple(roots)
    return size


def _signature(roots: Iterable[Path]) -> tuple[tuple[str, int], ...]:
    """Return mtimes of each root and its direct subdirs; adding or removing a package moves it."""
    entries: list[tuple[str, int]] = []
    for root in roots:
        entries.append((str(root), _mtime_ns(root)))
        try:
            it = os.scandir(root)
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        entries.append((entry.path, entry.stat(follow_symlinks=False).st_mtime_ns))
                except OSError:
                    continue
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


def _tree_size(root: Path, skip: Iterable[Path], seen: _Seen) -> int:
    """Sum regular-file sizes under *root*, not descending into *skip* or symlinks."""
    skip_set = {str(p) for p in skip}
    total = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            continue
        with it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.path not in skip_set:
                            stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                key = (stat.st_dev, stat.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += stat.st_size
    return total
