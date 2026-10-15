"""Files a device YAML pulls in: load tracking, normalisation and source reading."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import yaml
from esphome import yaml_util
from esphome.core import CORE, EsphomeError

from ...constants import is_secrets_file, secret_ref_names
from ..atomic_io import read_text_with_stat
from ..yaml import parse_yaml_fast_then_esphome
from ._parsing import (
    _digest_lines,
    _significant_lines,
    extract_config_content_fingerprint,
    safe_stat_key,
)

_LOGGER = logging.getLogger(__name__)

# Public from esphome/esphome#20805; earlier releases carry the same list as ``_load_listeners``.
_PUBLIC_LISTENERS: list[Callable[[Path], None]] | None = getattr(yaml_util, "load_listeners", None)
_LOAD_LISTENERS: list[Callable[[Path], None]] = (
    _PUBLIC_LISTENERS if _PUBLIC_LISTENERS is not None else yaml_util._load_listeners  # noqa: SLF001
)


# One permanent listener dispatching to the loading thread's recorder: the hook
# is process-global and other executor threads parse YAML concurrently (editor
# validation, secrets reads), and esphome's ``track_yaml_loads`` has no thread
# filter; a per-call listener would also race its own removal on older esphome.
class _Recorder(threading.local):
    loaded: list[str] | None = None


_RECORDER = _Recorder()


def _on_load(fname: Path) -> None:
    if (loaded := _RECORDER.loaded) is not None:
        loaded.append(os.fspath(fname))


_LOAD_LISTENERS.append(_on_load)


class DependencySources(NamedTuple):
    """What a device's dependency files contribute to its digest and pending-changes mtime."""

    lines: list[tuple[str, list[str]]]
    newest_mtime: float | None


class SourceText(NamedTuple):
    """One dependency file as read: a secrets file has ``secret_lines``, any other a ``digest``."""

    mtime: float
    stat_key: tuple[int, int]
    digest: str
    secret_refs: list[str]
    secret_lines: dict[str, str] | None


@dataclass(slots=True)
class LoadBatch:
    """State the devices of one config dir's scan batch share: reads and the data dir prefix."""

    config_dir: Path
    sources: dict[str, SourceText | None] = field(default_factory=dict)
    unreadable: set[str] = field(default_factory=set)
    # Stat keys the scanner took before the loads; a cache check needs no second stat.
    stat_keys: dict[str, tuple[int, int]] = field(default_factory=dict)
    _data_dir_prefix: str | None = None
    _dir: str = field(init=False)

    def __post_init__(self) -> None:
        self._dir = os.fspath(self.config_dir)

    def path_of(self, dependency: str) -> str:
        """*dependency* joined under the config dir; an absolute one stays as is."""
        return os.path.join(self._dir, dependency)  # noqa: PTH118

    def stat_key(self, dependency: str) -> tuple[int, int]:
        """*dependency*'s ``(mtime_ns, size)`` as read this batch, else from a fresh stat."""
        if dependency in self.sources:
            source = self.sources[dependency]
            return source.stat_key if source is not None else (0, 0)
        return safe_stat_key(self.path_of(dependency))

    def data_dir_prefix(self) -> str:
        """``CORE.data_dir`` as an absolute, normalised path prefix, resolved once per batch."""
        if self._data_dir_prefix is None:
            self._data_dir_prefix = (
                os.path.normcase(os.path.abspath(Path(CORE.data_dir))) + os.sep  # noqa: PTH100
            )
        return self._data_dir_prefix


@contextmanager
def track_loaded_files() -> Iterator[list[str]]:
    """Record every file esphome's YAML loader opens on this thread while active."""
    loaded: list[str] = []
    outer = _RECORDER.loaded
    _RECORDER.loaded = loaded
    try:
        yield loaded
    finally:
        _RECORDER.loaded = outer


def normalize_dependencies(path: Path, loaded: Iterable[str], data_prefix: str) -> tuple[str, ...]:
    """
    Sorted, deduplicated *loaded* paths, relative to *path*'s directory or absolute.

    Drops *path* itself and anything under *data_prefix* (``CORE.data_dir``, the git
    package cache). Paths compare lexically: every file the loader reports descends
    from *path*.
    """
    own = os.path.normcase(os.path.abspath(path))  # noqa: PTH100
    config_prefix = os.path.dirname(own) + os.sep  # noqa: PTH120
    dependencies: set[str] = set()
    for raw in set(loaded):
        normalized = os.path.normcase(os.path.abspath(raw))  # noqa: PTH100
        if normalized == own or normalized.startswith(data_prefix):
            continue
        if normalized.startswith(config_prefix):
            normalized = normalized.removeprefix(config_prefix).replace(os.sep, "/")
        dependencies.add(normalized)
    return tuple(sorted(dependencies))


def read_dependency_sources(
    dependencies: Iterable[str], yaml_content: str, batch: LoadBatch
) -> DependencySources | None:
    """
    Read *dependencies* (relative to the batch's config dir, or absolute) into digest lines.

    A file contributes its digest; a secrets file only the resolved values of the keys
    *yaml_content* and the other dependencies reference. ``newest_mtime`` spans the
    non-secret files. ``None`` when a file exists but cannot be read or parsed, so no
    digest is drawn from it.
    """
    lines: list[tuple[str, list[str]]] = []
    secrets: list[tuple[int, dict[str, str]]] = []
    refs: list[str] = []
    newest_mtime: float | None = None
    for dependency in dependencies:
        if dependency in batch.unreadable:
            return None
        if dependency in batch.sources:
            source = batch.sources[dependency]
        else:
            try:
                source = batch.sources[dependency] = _read_source(
                    batch.path_of(dependency),
                    secrets=is_secrets_file(dependency),
                    stat_key=batch.stat_keys.get(dependency),
                )
            except (OSError, UnicodeDecodeError, yaml.YAMLError, EsphomeError) as err:
                batch.unreadable.add(dependency)
                _LOGGER.warning(
                    "Could not read %s; edits to it will not refresh the devices that use it: %s",
                    dependency,
                    err,
                )
                return None
        if source is None:
            lines.append((dependency, []))
        elif source.secret_lines is not None:
            secrets.append((len(lines), source.secret_lines))
            lines.append((dependency, []))
        else:
            lines.append((dependency, [source.digest]))
            refs += source.secret_refs
            if newest_mtime is None or source.mtime > newest_mtime:
                newest_mtime = source.mtime
    if secrets:
        referenced = sorted({*secret_ref_names(yaml_content), *refs})
        for index, secret_lines in secrets:
            lines[index] = (
                lines[index][0],
                [secret_lines[n] for n in referenced if n in secret_lines],
            )
    return DependencySources(lines, newest_mtime)


def source_fingerprint(
    config_dir: Path, dependencies: tuple[str, ...] | None, yaml_content: str
) -> str:
    """Digest of *yaml_content* and *dependencies* as on disk now; empty when either is unknown."""
    if dependencies is None:
        return ""
    sources = read_dependency_sources(dependencies, yaml_content, LoadBatch(config_dir))
    return extract_config_content_fingerprint(yaml_content, sources.lines) if sources else ""


def read_source_fingerprint(config_path: Path, dependencies: tuple[str, ...] | None) -> str:
    """``source_fingerprint`` of *config_path*'s text; empty when the file cannot be read."""
    try:
        content = read_text_with_stat(config_path)[1]
    except (OSError, UnicodeDecodeError):
        _LOGGER.debug("Could not read %s", config_path, exc_info=True)
        return ""
    return source_fingerprint(config_path.parent, dependencies, content)


def _read_source(
    name: str, *, secrets: bool, stat_key: tuple[int, int] | None = None
) -> SourceText | None:
    """Read the file *name*; ``None`` when absent (esphome probes files that need not exist)."""
    if secrets:
        return _read_secrets(name, stat_key)
    try:
        stat, text = read_text_with_stat(Path(name))
    except FileNotFoundError:
        return None
    key = (stat.st_mtime_ns, stat.st_size)
    return SourceText(
        stat.st_mtime, key, _digest_lines(_significant_lines(text)), secret_ref_names(text), None
    )


# Parsed secrets files by path; every device load of a fleet reads the same one, so an
# unchanged file costs a stat instead of a read and a YAML parse.
_SECRETS_CACHE: dict[str, SourceText] = {}


def _read_secrets(name: str, stat_key: tuple[int, int] | None) -> SourceText | None:
    """Parse the secrets file *name* into lines, served from the cache while its stat key holds."""
    if stat_key is None:
        try:
            stat = os.stat(name)  # noqa: PTH116
        except FileNotFoundError:
            return None
        stat_key = (stat.st_mtime_ns, stat.st_size)
    cached = _SECRETS_CACHE.get(name)
    if cached is not None and cached.stat_key == stat_key:
        return cached
    path = Path(name)
    stat, text = read_text_with_stat(path)
    key = (stat.st_mtime_ns, stat.st_size)
    source = SourceText(stat.st_mtime, key, "", [], _secret_lines(path, text))
    _SECRETS_CACHE[name] = source
    return source


def _secret_lines(path: Path, text: str) -> dict[str, str]:
    """One ``name: value`` digest line per key of the secrets file; a non-mapping has none."""
    data = parse_yaml_fast_then_esphome(path, text)
    if not isinstance(data, dict):
        return {}
    return {
        str(name): f"{name}: {json.dumps(value, sort_keys=True, default=str)}"
        for name, value in data.items()
    }
