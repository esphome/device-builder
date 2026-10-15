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
    safe_stat_key,
)

DependencyStatKey = tuple[int, int]
DependencyStatKeys = tuple[DependencyStatKey, ...]

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
    for raw in loaded:
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
    read: list[tuple[str, tuple[float, str] | None]] = []
    for dependency in dependencies:
        try:
            stat, text = read_text_with_stat(config_dir / dependency)
        except (OSError, UnicodeDecodeError):
            read.append((dependency, None))
            continue
        read.append((dependency, (stat.st_mtime, text)))
    referenced: frozenset[str] = frozenset()
    if any(is_secrets_file(dependency) for dependency, _ in read):
        referenced = frozenset(
            name
            for text in (yaml_content, *(r[1] for d, r in read if r and not is_secrets_file(d)))
            for name in _SECRET_REF_RE.findall(text)
        )
    lines: list[tuple[str, list[str]]] = []
    for dependency, source in read:
        if source is None:
            lines.append((dependency, []))
        elif is_secrets_file(dependency):
            lines.append((dependency, _capture_top_level_lines(source[1], referenced)))
        else:
            lines.append((dependency, _significant_lines(source[1])))
    newest_mtime = max(
        (r[0] for d, r in read if r is not None and not is_secrets_file(d)), default=None
    )
    return DependencySources(lines, newest_mtime)


def dependency_fingerprint(
    config_dir: Path, dependencies: tuple[str, ...] | None, yaml_content: str
) -> str:
    """Versioned digest of *dependencies* as on disk now; empty when they are unknown."""
    if dependencies is None:
        return ""
    return extract_dependency_fingerprint(
        read_dependency_sources(config_dir, dependencies, yaml_content).lines
    )


def read_source_fingerprints(
    config_path: Path, dependencies: tuple[str, ...] | None
) -> SourceFingerprints:
    """Digests of *config_path* as on disk now; empty when unreadable, or unknown dependencies."""
    try:
        content = read_text_with_stat(config_path)[1]
    except (OSError, UnicodeDecodeError):
        return SourceFingerprints("", "")
    return SourceFingerprints(
        extract_config_content_fingerprint(content),
        dependency_fingerprint(config_path.parent, dependencies, content),
    )


def dependency_stat_keys(config_dir: Path, dependencies: Iterable[str]) -> DependencyStatKeys:
    """``safe_stat_key`` per dependency, in order."""
    return tuple(safe_stat_key(config_dir / dependency) for dependency in dependencies)
