"""Files a device YAML pulls in: load tracking, normalisation and source reading."""

from __future__ import annotations

import functools
import logging
import os
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from esphome import yaml_util
from esphome.core import CORE
from esphome.helpers import is_ha_addon

from ...constants import SECRET_REF_RE, is_secrets_file
from ..atomic_io import read_text_with_stat
from ._parsing import (
    _capture_top_level_lines,
    _has_version_prefix,
    _sha256_lines,
    _significant_lines,
    extract_config_content_fingerprint,
)

_LOGGER = logging.getLogger(__name__)

DependencyStatKey = tuple[int, int]
DependencyStatKeys = tuple[DependencyStatKey, ...]

# Prefix on every stored dependency digest; bump when the algorithm changes.
DEPENDENCY_FINGERPRINT_VERSION = "1:"


@dataclass(slots=True)
class DependencySources:
    """What a device's dependency files contribute to its digest and pending-changes mtime."""

    lines: list[tuple[str, list[str]]]
    newest_mtime: float | None


class SourceText(NamedTuple):
    """One dependency file as read: its mtime and text."""

    mtime: float
    text: str


# Per-batch read memo so a file shared by the fleet is read once; ``None`` is unreadable.
SourceMemo = dict[str, SourceText | None]


class SourceFingerprints(NamedTuple):
    """The content and dependency digests of one device YAML."""

    content: str
    dependency: str


@contextmanager
def track_loaded_files() -> Iterator[list[str]]:
    """Record every file esphome's YAML loader opens on this thread while active."""
    owner = threading.get_ident()
    loaded: list[str] = []

    def _on_load(fname: Path) -> None:
        # The hook is process-global and other executor threads parse YAML
        # concurrently (editor validation, secrets reads); only this thread's
        # loads belong to the device being tracked.
        if threading.get_ident() == owner:
            loaded.append(os.fspath(fname))

    # esphome's public ``track_yaml_loads`` has no thread filter.
    yaml_util._load_listeners.append(_on_load)  # noqa: SLF001
    try:
        yield loaded
    finally:
        yaml_util._load_listeners.remove(_on_load)  # noqa: SLF001


def normalize_dependencies(path: Path, loaded: Iterable[str]) -> tuple[str, ...]:
    """
    Sorted, deduplicated *loaded* paths, relative to *path*'s directory when inside it.

    Drops *path* itself and anything under ``CORE.data_dir`` (the git package cache).
    Paths compare lexically: every file the loader reports descends from *path*.
    """
    own = os.path.normcase(os.path.normpath(path))
    config_prefix = (os.path.dirname(own) or os.curdir) + os.sep  # noqa: PTH120
    data_prefix = _data_dir_prefix(
        config_path=CORE.config_path,
        ha_addon=is_ha_addon(),
        data_dir_env=os.environ.get("ESPHOME_DATA_DIR"),
    )
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
    config_dir: Path,
    dependencies: Iterable[str],
    yaml_content: str,
    memo: SourceMemo | None = None,
) -> DependencySources | None:
    """
    Read *dependencies* (relative to *config_dir*, or absolute) into digest lines.

    A secrets file contributes only the keys *yaml_content* and the other
    dependencies reference; ``newest_mtime`` spans the non-secret files.
    ``None`` when a file exists but cannot be read, so no digest is drawn from it.
    """
    if memo is None:
        memo = {}
    sources: list[tuple[str, bool, SourceText | None]] = []
    for dependency in dependencies:
        if dependency not in memo:
            try:
                memo[dependency] = _read_source(config_dir / dependency)
            except (OSError, UnicodeDecodeError):
                _LOGGER.debug("Could not read dependency %s", dependency, exc_info=True)
                return None
        sources.append((dependency, is_secrets_file(dependency), memo[dependency]))
    referenced = (
        _referenced_secrets(yaml_content, sources)
        if any(secret for _, secret, _ in sources)
        else frozenset()
    )
    lines: list[tuple[str, list[str]]] = []
    for dependency, secret, source in sources:
        if source is None:
            lines.append((dependency, []))
        elif secret:
            lines.append((dependency, _capture_top_level_lines(source.text, referenced)))
        else:
            lines.append((dependency, _significant_lines(source.text)))
    newest_mtime = max(
        (source.mtime for _, secret, source in sources if source is not None and not secret),
        default=None,
    )
    return DependencySources(lines, newest_mtime)


def extract_dependency_fingerprint(sources: Iterable[tuple[str, Iterable[str]]]) -> str:
    """Versioned digest of each dependency's path and significant lines."""
    lines = [line for dependency, body in sources for line in (dependency, *body)]
    return f"{DEPENDENCY_FINGERPRINT_VERSION}{_sha256_lines(lines)}"


def dependency_fingerprint_is_current(stored: object) -> bool:
    """Return True when *stored* is a dependency digest the current algorithm produced."""
    return _has_version_prefix(stored, DEPENDENCY_FINGERPRINT_VERSION)


def dependency_stamp_matches(stamped: object, current: str) -> bool:
    """Whether a stamped dependency digest covers *current*; an absent stamp covers any."""
    return stamped is None or stamped == current


def dependency_fingerprint(
    config_dir: Path, dependencies: tuple[str, ...] | None, yaml_content: str
) -> str:
    """Versioned digest of *dependencies* as on disk now; empty when they are unknown."""
    if dependencies is None:
        return ""
    sources = read_dependency_sources(config_dir, dependencies, yaml_content)
    return extract_dependency_fingerprint(sources.lines) if sources is not None else ""


def read_source_fingerprints(
    config_path: Path, dependencies: tuple[str, ...] | None
) -> SourceFingerprints:
    """Digests of *config_path* as on disk now; empty when unreadable, or unknown dependencies."""
    try:
        content = read_text_with_stat(config_path)[1]
    except (OSError, UnicodeDecodeError):
        _LOGGER.debug("Could not read %s", config_path, exc_info=True)
        return SourceFingerprints("", "")
    return SourceFingerprints(
        extract_config_content_fingerprint(content),
        dependency_fingerprint(config_path.parent, dependencies, content),
    )


def _referenced_secrets(
    yaml_content: str, sources: Iterable[tuple[str, bool, SourceText | None]]
) -> frozenset[str]:
    """``!secret`` names *yaml_content* and the non-secret *sources* reference."""
    names = SECRET_REF_RE.findall(yaml_content)
    for _, secret, source in sources:
        if source is not None and not secret:
            names += SECRET_REF_RE.findall(source.text)
    return frozenset(names)


def _read_source(path: Path) -> SourceText | None:
    """*path* as read, or ``None`` when absent (esphome probes files that need not exist)."""
    try:
        stat, text = read_text_with_stat(path)
    except FileNotFoundError:
        return None
    return SourceText(stat.st_mtime, text)


@functools.lru_cache(maxsize=1)
def _data_dir_prefix(*, config_path: Path | None, ha_addon: bool, data_dir_env: str | None) -> str:
    """Normalised ``CORE.data_dir`` prefix, keyed on every input esphome resolves it from."""
    return os.path.normcase(os.path.normpath(Path(CORE.data_dir))) + os.sep
