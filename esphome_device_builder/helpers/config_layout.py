"""
Which YAMLs under the config dir are devices, which are packages, and which are neither.

A device sits at the top level or anywhere below the top-level ``devices/`` folder; every
other YAML in a subfolder is a package. Secrets files (``secrets.yaml``, ``secrets.yml``,
``secrets.<anything>.yaml``) are neither, at any depth. Hidden files and folders, the
top-level ``archive/`` folder and symlinked folders are never walked.
"""

from __future__ import annotations

import logging
import os
import posixpath
from collections.abc import Callable, Iterator
from pathlib import Path

from ..constants import SECRETS_FILENAME
from ..models import ErrorCode
from .api import CommandError
from .cross_os_path import cross_os_basename
from .paths import PathEscapeError, resolve_under_root

_LOGGER = logging.getLogger(__name__)

# The top-level folder whose YAMLs, at any depth, are devices. Matched exactly.
DEVICES_DIR = "devices"
# The dashboard's archived device YAMLs live here; they are devices, not packages.
ARCHIVE_DIR = "archive"
YAML_SUFFIXES = (".yaml", ".yml")
# Never in a canonical segment: a Windows separator, an NTFS stream suffix (``::$DATA``)
# or drive (``C:``), and a NUL.
_NON_CANONICAL_CHARS = frozenset("\\:\x00")
# Folders below devices/ the walk skipped, so each is logged once per process.
_WARNED_FOLDERS: set[str] = set()


def is_secrets_like_name(name: str) -> bool:
    """Whether *name*'s basename is ``secrets.yaml``, ``secrets.yml`` or ``secrets.<x>.yaml``."""
    folded = cross_os_basename(name).rstrip(". ").casefold()
    return folded.startswith("secrets.") and folded.endswith(YAML_SUFFIXES)


def canonical_rel(rel: str) -> str:
    """Return config-relative *rel* lexically normalised to POSIX (``./x``, ``a/../x`` folded)."""
    return posixpath.normpath(rel.replace(os.sep, "/"))


def in_devices_dir(rel: str) -> bool:
    """
    Whether config-relative *rel* may name a file below the top-level ``devices/`` folder.

    A guard, so it errs wide: *rel* is normalised first and the folder name is compared
    case-insensitively, so no spelling that reaches a nested device (``./devices/x.yaml``,
    ``DEVICES/x.yaml`` on a case-insensitive filesystem) slips past it.
    """
    parts = canonical_rel(rel).split("/")
    return len(parts) > 1 and parts[0].casefold() == DEVICES_DIR


def config_relative(config_dir: Path, path: Path) -> str:
    """
    Return *path*, a file below *config_dir*, as a config-relative POSIX string.

    Lexical, like ``Path.relative_to``, which the per-device scan avoids for its cost.
    Raises ``ValueError`` when *path* is not below *config_dir*.
    """
    root = config_dir.parts
    parts = path.parts
    if len(parts) <= len(root) or parts[: len(root)] != root:
        msg = f"{str(path)!r} is not below {str(config_dir)!r}"
        raise ValueError(msg)
    return "/".join(parts[len(root) :])


def sibling_secrets(configuration: str) -> str:
    """Return the config-relative secrets file that ``!secret`` in *configuration* reads."""
    if "/" not in configuration:
        return SECRETS_FILENAME  # the common, top-level case, once per device per scan
    return posixpath.join(posixpath.dirname(configuration), SECRETS_FILENAME)


def is_canonical_configuration(configuration: str) -> bool:
    """
    Whether *configuration* is spelled the one way ``Device.configuration`` spells a file.

    That is a bare top-level name or a path below ``devices/``: relative, POSIX
    separators, no empty, ``.`` or ``..`` segment, no hidden folder, and no segment
    with a NUL, a ``:``, a backslash or the trailing dot or space Win32 strips (so
    ``devices./x.yaml`` can't alias ``devices/x.yaml``). Only the spelling is checked,
    not what the file is.
    """
    parts = configuration.split("/")
    return (
        all(_is_canonical_segment(part) for part in parts)
        and not any(part.startswith(".") for part in parts[:-1])
        and (len(parts) == 1 or parts[0] == DEVICES_DIR)
    )


def canonical_configuration(config_dir: Path, configuration: str) -> str:
    """
    Return the config-relative POSIX form of a wire *configuration*. Blocking.

    Its folders are resolved through ``..``, absolute paths and symlinks, so every spelling
    of one device file (``./x.yaml``, ``x.yaml/``, an absolute path, a folder symlink onto
    ``devices/lights``) maps to the ``Device.configuration`` the scanner lists it under. The
    file name is kept as given: a top-level symlink is a device of its own, and StorageJSON is
    keyed by that name. One whose folder resolves outside *config_dir* is only normalised
    lexically; ``DashboardSettings.rel_path`` refuses it.
    """
    path = config_dir / configuration
    try:
        folder = resolve_under_root(path.parent, config_dir)
    except PathEscapeError:
        return canonical_rel(configuration)
    rel = folder.relative_to(config_dir.resolve()) / path.name
    return canonical_rel(rel.as_posix())


def is_nested_device(config_dir: Path, configuration: str) -> bool:
    """Whether *configuration*, however spelled, names a file below ``devices/``. Blocking."""
    return in_devices_dir(canonical_configuration(config_dir, configuration))


def refuse_nested_device(config_dir: Path, configuration: str, action: str) -> None:
    """Raise ``INVALID_ARGS`` naming *action* for a file below ``devices/``. Blocking."""
    if is_nested_device(config_dir, configuration):
        msg = (
            f"{action} is not supported yet for a device inside the {DEVICES_DIR}/ folder "
            f"({configuration})"
        )
        raise CommandError(ErrorCode.INVALID_ARGS, msg)


def refuse_path_in_name(new_name: str) -> None:
    """Raise ``INVALID_ARGS`` when a new device *new_name* carries a folder separator."""
    if "/" in new_name or "\\" in new_name:
        msg = f"new_name must be a device name, not a path: {new_name!r}"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)


def is_device_path(rel: str) -> bool:
    """Whether the config-relative POSIX *rel* names a device YAML."""
    parts = rel.split("/")
    return (
        _is_visible_yaml(parts)
        and not is_secrets_like_name(parts[-1])
        and (len(parts) == 1 or parts[0] == DEVICES_DIR)
    )


def is_package_path(rel: str) -> bool:
    """Whether the config-relative POSIX *rel* names a package YAML."""
    parts = rel.split("/")
    return (
        len(parts) > 1
        and _is_visible_yaml(parts)
        and not is_secrets_like_name(parts[-1])
        # Folded, as parse_file_path folds it: ``Devices/x.yaml`` is no package files/* takes.
        and parts[0].casefold() not in (DEVICES_DIR, ARCHIVE_DIR)
    )


def is_device_config_name(configuration: str | Path) -> bool:
    """Whether *configuration* canonically names a device YAML, free of Win32 aliases."""
    raw = str(configuration)
    # is_device_path wants a .yaml / .yml extension, ruling out the 8.3 alias SECRET~1.YAM.
    return is_canonical_configuration(raw) and is_device_path(raw)


def resolved_rel(config_dir: Path, rel: str) -> str | None:
    """
    Return the config-relative POSIX path *rel* resolves to through symlinks. Blocking.

    ``None`` when it resolves outside *config_dir* or can't be resolved.
    """
    try:
        resolved = resolve_under_root(config_dir / rel, config_dir)
    except PathEscapeError:
        return None
    return resolved.relative_to(config_dir.resolve()).as_posix()


def iter_device_files(config_dir: Path) -> list[str]:
    """
    Every device YAML under *config_dir*, POSIX-relative and sorted. Blocking.

    A failure listing *config_dir* itself raises; one below it is logged and skipped.
    """
    # The walk only yields visible YAMLs at the top level or below devices/, the rest of
    # is_device_path, so only the secrets check is left: this runs per file on every scan.
    return sorted(
        rel for rel in _walk(config_dir, _descend_devices) if not is_secrets_like_name(rel)
    )


def _descend_devices(top: bool, name: str) -> bool:  # noqa: FBT001 - _walk's callback shape
    """Whether the device walk enters folder *name*: ``devices/``, and canonical names below."""
    if top:
        return name == DEVICES_DIR
    if _is_canonical_segment(name):
        return True
    # A device there would get a configuration no command takes; say so once.
    if name not in _WARNED_FOLDERS:
        _WARNED_FOLDERS.add(name)
        _LOGGER.warning(
            "Skipping folder %r below %s/: a name with a ':', a backslash or a trailing "
            "dot or space can't hold devices",
            name,
            DEVICES_DIR,
        )
    return False


def name_shared_by_another_device(config_dir: Path, configuration: str) -> bool:
    """Whether a device file other than *configuration*, however spelled, has its name. Blocking."""
    own = canonical_configuration(config_dir, configuration)
    name = posixpath.basename(own)
    return any(
        posixpath.basename(rel) == name and rel != own for rel in iter_device_files(config_dir)
    )


def refuse_device_name_clash(config_dir: Path, configuration: str) -> None:
    """
    Raise ``INVALID_ARGS`` when writing *configuration* would create a name twin. Blocking.

    Only for a device file that doesn't exist yet: StorageJSON and the build cache are keyed
    by file name, so a second device file of that name turns both into placeholders.
    Writes to an existing file, and to anything not a device (secrets.yaml), pass.
    """
    own = canonical_configuration(config_dir, configuration)
    if not is_device_path(own) or (config_dir / own).exists():
        return
    name = posixpath.basename(own)
    for rel in iter_device_files(config_dir):
        if posixpath.basename(rel) == name:
            msg = f"A device named {name} already exists ({rel}); device file names must be unique"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)


def refuse_nested_name_clash(config_dir: Path, filename: str) -> None:
    """
    Raise ``INVALID_ARGS`` when a device below ``devices/`` already has *filename*. Blocking.

    For the paths that create a top-level device: StorageJSON and the build cache are keyed
    by file name, so the new file would turn both into unloadable placeholders.
    """
    name = posixpath.basename(canonical_rel(filename))
    for rel in iter_device_files(config_dir):
        if in_devices_dir(rel) and posixpath.basename(rel) == name:
            msg = f"A device named {name} already exists ({rel}); device file names must be unique"
            raise CommandError(ErrorCode.INVALID_ARGS, msg)


def iter_config_yamls(config_dir: Path) -> Iterator[str]:
    """Yield each visible YAML under *config_dir* outside ``archive/``, POSIX-relative. Blocking."""
    return _walk(config_dir, _outside_archive)


def iter_referrer_yamls(config_dir: Path) -> Iterator[str]:
    """
    Yield each YAML that may include another, POSIX-relative. Blocking.

    :func:`iter_config_yamls` plus the hidden files (``.shared.yaml``,
    ``common/.wrapper.yaml``) beside them: no device or package, but a device can
    include one. Hidden folders stay unwalked, and a hidden file that resolves
    outside *config_dir* is left out.
    """
    return _walk(config_dir, _outside_archive, hidden_files=True)


def _outside_archive(top: bool, name: str) -> bool:  # noqa: FBT001 - _walk's callback shape
    """Whether the walk enters folder *name*: any but the top-level ``archive/``."""
    return not (top and name.casefold() == ARCHIVE_DIR)


def _walk(
    config_dir: Path, descend: Callable[[bool, str], bool], *, hidden_files: bool = False
) -> Iterator[str]:
    """Yield the YAMLs of *config_dir* and of the visible folders *descend* admits."""
    root = config_dir.resolve()
    top_dir = os.fspath(config_dir)

    def _on_error(err: OSError) -> None:
        if err.filename == top_dir:
            raise err
        _LOGGER.warning("Can't list %s: %s", err.filename, err)

    # os.walk does not descend into symlinked dirs (followlinks=False).
    for dirpath, dirnames, filenames in os.walk(config_dir, onerror=_on_error):
        # Plain string slicing, not Path: this runs for every file of every scan. os.walk
        # joins each dirpath onto top_dir, so the rest is the folder's config-relative path.
        rel_dir = dirpath[len(top_dir) :].lstrip(os.sep).replace(os.sep, "/")
        top = not rel_dir
        prefix = f"{rel_dir}/" if rel_dir else ""
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and descend(top, d))
        for name in filenames:
            hidden = name.startswith(".")
            if (hidden and not hidden_files) or not name.lower().endswith(YAML_SUFFIXES):
                continue
            # A visible top-level YAML is taken as is, symlink or not, as esphome's own
            # listing does.
            if hidden or not top:
                full = Path(dirpath, name)
                if full.is_symlink():
                    try:
                        resolve_under_root(full, root)
                    except PathEscapeError:
                        continue
            yield prefix + name


def _is_canonical_segment(part: str) -> bool:
    """Whether *part* is one segment Win32 and POSIX both read as written."""
    return bool(part) and part.rstrip(". ") == part and _NON_CANONICAL_CHARS.isdisjoint(part)


def _is_visible_yaml(parts: list[str]) -> bool:
    """Whether *parts* are all non-empty, none hidden, and end in a YAML name."""
    return all(part and not part.startswith(".") for part in parts) and parts[-1].lower().endswith(
        YAML_SUFFIXES
    )
