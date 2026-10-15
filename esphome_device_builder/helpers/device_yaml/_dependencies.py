"""Files a device YAML pulls in: load tracking, normalisation and source reading."""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from esphome import yaml_util
from esphome.core import CORE

from ...constants import is_secrets_file
from ..atomic_io import read_text_with_stat
from ._parsing import (
    _capture_top_level_lines,
    _significant_lines,
    extract_config_content_fingerprint,
    extract_dependency_fingerprint,
)

DependencyStatKey = tuple[int, int]
DependencyStatKeys = tuple[DependencyStatKey, ...]

UNREADABLE_STAT_KEY: DependencyStatKey = (-1, -1)

_SECRET_REF_RE = re.compile(r"!secret\s+([^\s#]+)")


@dataclass(slots=True)
class DependencySources:
    """What a device's dependency files contribute to its digest and pending-changes mtime."""

    lines: list[tuple[str, list[str]]]
    newest_mtime: float | None


class SourceFingerprints(NamedTuple):
    """The content and dependency digests of one device YAML."""

    content: str
    dependency: str


@contextmanager
def track_loaded_files() -> Iterator[list[Path]]:
    """Record every file esphome's YAML loader opens on this thread while active."""
    owner = threading.get_ident()
    loaded: list[Path] = []

    def _on_load(fname: Path) -> None:
        # The hook is process-global and other executor threads parse YAML
        # concurrently (editor validation, secrets reads); only this thread's
        # loads belong to the device being tracked.
        if threading.get_ident() == owner:
            loaded.append(Path(fname))

    # esphome's public ``track_yaml_loads`` has no thread filter.
    yaml_util._load_listeners.append(_on_load)  # noqa: SLF001
    try:
        yield loaded
    finally:
        yaml_util._load_listeners.remove(_on_load)  # noqa: SLF001


def normalize_dependencies(path: Path, loaded: Iterable[Path]) -> tuple[str, ...]:
    """
    Sorted, deduplicated *loaded* paths as strings, relative to *path*'s directory when inside it.

    Drops *path* itself and anything under ``CORE.data_dir`` (the git package cache).
    Paths compare lexically: every file the loader reports descends from *path*.
    """
    own = os.path.normcase(os.path.normpath(path))
    config_prefix = str(Path(own).parent) + os.sep
    data_prefix = os.path.normcase(os.path.normpath(CORE.data_dir)) + os.sep
    dependencies: set[str] = set()
    for raw in set(loaded):
        normalized = os.path.normcase(os.path.normpath(raw))
        if normalized == own or normalized.startswith(data_prefix):
            continue
        if normalized.startswith(config_prefix):
            normalized = normalized.removeprefix(config_prefix).replace(os.sep, "/")
        dependencies.add(normalized)
    return tuple(sorted(dependencies))


def read_dependency_sources(
    config_dir: Path, dependencies: Iterable[str], yaml_content: str
) -> DependencySources:
    """
    Read *dependencies* (relative to *config_dir*, or absolute) into digest lines.

    A secrets file contributes only the keys *yaml_content* and the other
    dependencies reference; ``newest_mtime`` spans the non-secret files.
    """
    read: list[tuple[str, str | None, os.stat_result | None]] = []
    for dependency in dependencies:
        try:
            stat, text = read_text_with_stat(config_dir / dependency)
        except (OSError, UnicodeDecodeError):
            read.append((dependency, None, None))
            continue
        read.append((dependency, text, stat))
    referenced = frozenset(
        _SECRET_REF_RE.findall(
            "\n".join(
                [yaml_content]
                + [text for dep, text, _ in read if text is not None and not is_secrets_file(dep)]
            )
        )
    )
    lines: list[tuple[str, list[str]]] = []
    newest_mtime: float | None = None
    for dependency, content, stat_result in read:
        if content is None:
            lines.append((dependency, []))
        elif is_secrets_file(dependency):
            lines.append((dependency, _capture_top_level_lines(content, referenced)))
        else:
            lines.append((dependency, _significant_lines(content)))
            if stat_result is not None and (
                newest_mtime is None or stat_result.st_mtime > newest_mtime
            ):
                newest_mtime = stat_result.st_mtime
    return DependencySources(lines, newest_mtime)


def read_source_fingerprints(
    config_path: Path, dependencies: tuple[str, ...] | None
) -> SourceFingerprints:
    """Digests of *config_path* as on disk now; empty when unreadable, or unknown dependencies."""
    try:
        content = read_text_with_stat(config_path)[1]
    except (OSError, UnicodeDecodeError):
        return SourceFingerprints("", "")
    dependency = (
        extract_dependency_fingerprint(
            read_dependency_sources(config_path.parent, dependencies, content).lines
        )
        if dependencies is not None
        else ""
    )
    return SourceFingerprints(extract_config_content_fingerprint(content), dependency)


def dependency_stat_keys(config_dir: Path, dependencies: Iterable[str]) -> DependencyStatKeys:
    """``(mtime_ns, size)`` per dependency, in order; unreadable files key as ``(-1, -1)``."""
    keys: list[DependencyStatKey] = []
    for dependency in dependencies:
        try:
            stat = (config_dir / dependency).stat()
        except OSError:
            keys.append(UNREADABLE_STAT_KEY)
            continue
        keys.append((stat.st_mtime_ns, stat.st_size))
    return tuple(keys)
