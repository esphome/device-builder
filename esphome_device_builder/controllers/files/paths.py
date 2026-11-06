"""Validation and discovery of non-device YAML files under the config dir."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from ...constants import is_secrets_file
from ...helpers.api import CommandError
from ...helpers.paths import PathEscapeError, resolve_under_root
from ...models import ErrorCode

_LOGGER = logging.getLogger(__name__)

# The dashboard's archived device YAMLs live here; they are devices, not packages.
ARCHIVE_DIR = "archive"
_YAML_SUFFIXES = (".yaml", ".yml")
# Whitespace, comment, quote and flow characters anywhere (``:`` is refused for
# all paths), ``$`` (ESPHome expands ``$var`` in an include path), or a segment
# not starting with a letter, digit or ``_`` (``@``, ``!``, ``&``, ``*`` … can't
# begin a plain YAML scalar).
_YAML_SIGNIFICANT_RE = re.compile(r"""[\s#"'{}\[\],$]|(?:^|/)[^\w/]""")


def parse_file_path(path: Any, *, in_subfolder: bool, plain_name: bool = False) -> str:
    """
    Validate a config-relative YAML *path* from the wire; return its POSIX form.

    Refused with ``INVALID_ARGS``: absolute paths, ``..``, hidden segments,
    non-YAML names, secrets files, the archive folder, with *in_subfolder*
    a top-level file (that is a device) and, with *plain_name*, names that
    can't be written verbatim in an ``!include``. Symlink escapes are left to
    ``DashboardSettings.rel_path``.
    """
    if not isinstance(path, str) or not path or "\\" in path or ":" in path or "\x00" in path:
        _invalid(path)
    pure = PurePosixPath(path)
    parts = pure.parts
    if (
        pure.is_absolute()
        or any(part == ".." or part.startswith(".") for part in parts)
        or pure.suffix.lower() not in _YAML_SUFFIXES
        or is_secrets_file(pure.name)
        or (len(parts) > 1 and parts[0].casefold() == ARCHIVE_DIR)
        or (in_subfolder and len(parts) < 2)
        or (plain_name and (_YAML_SIGNIFICANT_RE.search(path) or not path.isprintable()))
    ):
        _invalid(path)
    return pure.as_posix()


def list_package_files(config_dir: Path) -> list[str]:
    """Return every YAML below a subfolder of *config_dir*, POSIX-relative and sorted. Blocking."""
    return sorted(rel for rel in iter_subfolder_yamls(config_dir) if not is_secrets_file(rel))


def iter_subfolder_yamls(config_dir: Path) -> Iterator[str]:
    """Yield every YAML below a subfolder of *config_dir*, secrets files included. Blocking."""
    root = config_dir.resolve()
    # os.walk does not descend into symlinked dirs (followlinks=False).
    for dirpath, dirnames, filenames in os.walk(config_dir, onerror=_log_walk_error):
        rel_dir = Path(dirpath).relative_to(config_dir)
        top = rel_dir == Path()
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and not (top and d.casefold() == ARCHIVE_DIR)
        ]
        if top:
            continue
        for name in filenames:
            if name.startswith(".") or not name.lower().endswith(_YAML_SUFFIXES):
                continue
            full = Path(dirpath, name)
            if full.is_symlink():
                try:
                    resolve_under_root(full, root)
                except PathEscapeError:
                    continue
            yield (rel_dir / name).as_posix()


def list_archived_devices(config_dir: Path) -> list[str]:
    """Return every archived device YAML (``archive/<name>``), sorted. Blocking."""
    archive = config_dir / ARCHIVE_DIR
    if not archive.is_dir():
        return []
    names = _contained_yamls(archive, config_dir, hidden=False)
    return [f"{ARCHIVE_DIR}/{name}" for name in names]


def list_hidden_yamls(config_dir: Path) -> list[str]:
    """Return every top-level YAML whose name starts with a dot, sorted. Blocking."""
    return _contained_yamls(config_dir, config_dir, hidden=True)


def _contained_yamls(folder: Path, config_dir: Path, *, hidden: bool) -> list[str]:
    """Return the sorted names of *folder*'s (hidden or visible) YAMLs inside *config_dir*."""
    root = config_dir.resolve()
    found: list[str] = []
    for path in folder.iterdir():
        if (
            path.name.startswith(".") != hidden
            or not path.name.lower().endswith(_YAML_SUFFIXES)
            or not path.is_file()
        ):
            continue
        try:
            resolve_under_root(path, root)
        except PathEscapeError:
            continue
        found.append(path.name)
    return sorted(found)


def dependency_key(path: str) -> str:
    """*path* in the form ``Device.dependencies`` stores (case-folded on Windows)."""
    return os.path.normcase(path).replace(os.sep, "/")


def _log_walk_error(err: OSError) -> None:
    _LOGGER.warning("Can't list %s for packages: %s", err.filename, err)


def _invalid(path: Any) -> NoReturn:
    raise CommandError(ErrorCode.INVALID_ARGS, f"invalid file path: {path!r}")
