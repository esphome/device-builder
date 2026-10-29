"""Validation and discovery of non-device YAML files under the config dir."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from ...constants import is_secrets_file
from ...helpers.api import CommandError
from ...helpers.paths import PathEscapeError, resolve_under_root
from ...models import ErrorCode

# The dashboard's archived device YAMLs live here; they are devices, not packages.
ARCHIVE_DIR = "archive"
_YAML_SUFFIXES = (".yaml", ".yml")
# Whitespace, comment, quote and flow characters anywhere (``:`` is refused for
# all paths), or a segment not starting with a letter, digit or ``_`` (``@``,
# ``!``, ``&``, ``*`` … can't begin a plain YAML scalar).
_YAML_SIGNIFICANT_RE = re.compile(r"""[\s#"'{}\[\],]|(?:^|/)[^\w/]""")


def parse_file_path(path: Any, *, in_subfolder: bool, plain_name: bool = False) -> str:
    """
    Validate a config-relative YAML *path* from the wire; return its POSIX form.

    Refused with ``INVALID_ARGS``: absolute paths, ``..``, hidden segments,
    non-YAML names, secrets files, the archive folder, with *in_subfolder*
    a top-level file (that is a device) and, with *plain_name*, names that
    would need quoting in an ``!include``. Symlink escapes are left to
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
        or (plain_name and _YAML_SIGNIFICANT_RE.search(path))
    ):
        _invalid(path)
    return pure.as_posix()


def list_package_files(config_dir: Path) -> list[str]:
    """Return every YAML below a subfolder of *config_dir*, POSIX-relative and sorted. Blocking."""
    root = config_dir.resolve()
    found: list[str] = []
    # os.walk does not descend into symlinked dirs (followlinks=False).
    for dirpath, dirnames, filenames in os.walk(config_dir):
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
            if (
                name.startswith(".")
                or not name.lower().endswith(_YAML_SUFFIXES)
                or is_secrets_file(name)
            ):
                continue
            full = Path(dirpath, name)
            if full.is_symlink():
                try:
                    resolve_under_root(full, root)
                except PathEscapeError:
                    continue
            found.append((rel_dir / name).as_posix())
    return sorted(found)


def list_archived_devices(config_dir: Path) -> list[str]:
    """Return every archived device YAML (``archive/<name>``), sorted. Blocking."""
    archive = config_dir / ARCHIVE_DIR
    if not archive.is_dir():
        return []
    root = config_dir.resolve()
    found: list[str] = []
    for path in archive.iterdir():
        if (
            path.name.startswith(".")
            or not path.name.lower().endswith(_YAML_SUFFIXES)
            or not path.is_file()
        ):
            continue
        try:
            resolve_under_root(path, root)
        except PathEscapeError:
            continue
        found.append(f"{ARCHIVE_DIR}/{path.name}")
    return sorted(found)


def dependency_key(path: str) -> str:
    """*path* in the form ``Device.dependencies`` stores (case-folded on Windows)."""
    return os.path.normcase(path).replace(os.sep, "/")


def _invalid(path: Any) -> NoReturn:
    raise CommandError(ErrorCode.INVALID_ARGS, f"invalid file path: {path!r}")
