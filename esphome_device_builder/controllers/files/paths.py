"""Validation and discovery of non-device YAML files under the config dir."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, NoReturn

from ...helpers.api import CommandError
from ...helpers.config_layout import (
    ARCHIVE_DIR,
    YAML_SUFFIXES,
    in_devices_dir,
    is_package_path,
    is_secrets_like_name,
    iter_config_yamls,
)
from ...helpers.paths import PathEscapeError, resolve_under_root
from ...models import ErrorCode

if TYPE_CHECKING:
    from ..config.settings import DashboardSettings

# Whitespace, comment, quote and flow characters anywhere (``:`` is refused for
# all paths), ``$`` (ESPHome expands ``$var`` in an include path), or a segment
# not starting with a letter, digit or ``_`` (``@``, ``!``, ``&``, ``*`` … can't
# begin a plain YAML scalar).
_YAML_SIGNIFICANT_RE = re.compile(r"""[\s#"'{}\[\],$]|(?:^|/)[^\w/]""")


def parse_file_path(path: Any, *, in_subfolder: bool, plain_name: bool = False) -> str:
    """
    Validate a config-relative YAML *path* from the wire; return its POSIX form.

    Refused with ``INVALID_ARGS``: absolute paths, ``..``, hidden segments,
    non-YAML names, secrets files (``secrets.<x>.yaml`` too), the archive folder,
    with *in_subfolder* a device path (top level or under ``devices/``) and,
    with *plain_name*, names that can't be written verbatim in an ``!include``.
    Symlinks are left to :func:`resolve_file_path`.
    """
    if not isinstance(path, str) or not path or "\\" in path or ":" in path or "\x00" in path:
        _invalid(path)
    pure = PurePosixPath(path)
    parts = pure.parts
    if (
        pure.is_absolute()
        or any(part == ".." or part.startswith(".") for part in parts)
        or pure.suffix.lower() not in YAML_SUFFIXES
        or is_secrets_like_name(pure.name)
        or (len(parts) > 1 and parts[0].casefold() == ARCHIVE_DIR)
        or (in_subfolder and (len(parts) < 2 or in_devices_dir(pure.as_posix())))
        or (plain_name and (_YAML_SIGNIFICANT_RE.search(path) or not path.isprintable()))
    ):
        _invalid(path)
    return pure.as_posix()


def resolve_file_path(settings: DashboardSettings, rel: str) -> Path:
    """
    Return ``settings.rel_path(rel)`` for a *rel* :func:`parse_file_path` took. Blocking.

    Refused with ``INVALID_ARGS`` unless the file it resolves to through symlinks passes
    the same guards, so a symlinked folder inside the config dir can't reach a device,
    a secrets file, ``archive/`` or a hidden folder.
    """
    target = settings.rel_path(rel)
    real = target.resolve().relative_to(settings.config_dir.resolve()).as_posix()
    try:
        parse_file_path(real, in_subfolder=True)
    except CommandError:
        _invalid(rel)
    return target


def list_package_files(config_dir: Path) -> list[str]:
    """Return every package YAML of *config_dir*, POSIX-relative and sorted. Blocking."""
    return sorted(rel for rel in iter_config_yamls(config_dir) if is_package_path(rel))


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
            or not path.name.lower().endswith(YAML_SUFFIXES)
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
