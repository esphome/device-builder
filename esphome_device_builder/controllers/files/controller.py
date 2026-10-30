"""Files controller — package / include YAMLs that are not devices."""

from __future__ import annotations

import logging
import posixpath
from typing import TYPE_CHECKING, Any

from ...constants import is_secrets_file
from ...helpers.api import CommandError, api_command
from ...helpers.async_ import run_in_executor
from ...helpers.atomic_io import atomic_write_exclusive
from ...helpers.device_config import raise_device_not_found
from ...helpers.yaml import write_user_yaml
from ...models import ErrorCode
from ..devices.file_change import commit_file_change
from ..devices.helpers import refuse_empty_write
from .paths import ARCHIVE_DIR, dependency_key, list_package_files, parse_file_path
from .rename import RenamePlan, find_referrers, plan_rename

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ..config.settings import DashboardSettings
    from ..devices import DevicesController

_LOGGER = logging.getLogger(__name__)


class FilesController:
    """
    List, create, delete and trace usage of non-device YAMLs.

    Reads and edits go through ``devices/get_config`` / ``devices/update_config``,
    which already take any config-relative path. Usage comes from the
    scanner's per-device dependency lists; nothing is stored here.
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder

    @api_command("files/list_packages")
    async def list_packages(self, **kwargs: Any) -> list[dict[str, str]]:
        """Return every package YAML (a YAML in a subfolder), sorted by path."""
        paths = await run_in_executor(list_package_files, self._db.settings.config_dir)
        return [{"path": path} for path in paths]

    @api_command("files/create")
    async def create(self, *, path: Any, content: Any, **kwargs: Any) -> dict[str, str]:
        """Create a package YAML in a subfolder (parents included); ``ALREADY_EXISTS`` if taken."""
        rel = parse_file_path(path, in_subfolder=True, plain_name=True)
        if not isinstance(content, str):
            raise CommandError(ErrorCode.INVALID_ARGS, "content must be a string")
        if not content.strip():
            refuse_empty_write(rel)
        settings = self._db.settings

        def _check() -> None:
            # rel_path resolves symlinks (blocking), so it runs here, not on the loop.
            if settings.rel_path(rel).exists():
                raise CommandError(ErrorCode.ALREADY_EXISTS, f"{rel!r} already exists")

        def _create() -> None:
            target = settings.rel_path(rel)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError) as err:
                msg = f"a parent of {rel!r} is not a folder"
                raise CommandError(ErrorCode.INVALID_ARGS, msg) from err
            try:
                atomic_write_exclusive(target, content.encode("utf-8"))
            except FileExistsError as err:
                raise CommandError(ErrorCode.ALREADY_EXISTS, f"{rel!r} already exists") from err

        # Nothing to snapshot: the file doesn't exist yet.
        await commit_file_change(
            self._devices,
            [rel],
            _create,
            message=f"Create {rel}",
            precheck=_check,
            snapshot=False,
        )
        return {"path": rel}

    @api_command("files/delete")
    async def delete(self, *, path: Any, **kwargs: Any) -> None:
        """Delete a package YAML."""
        rel = parse_file_path(path, in_subfolder=True)
        settings = self._db.settings

        def _check() -> None:
            target = settings.rel_path(rel)
            if target.is_dir():
                raise CommandError(ErrorCode.INVALID_ARGS, f"{rel!r} is a folder")
            if not target.exists():
                raise CommandError(ErrorCode.NOT_FOUND, f"{rel!r} not found")

        def _delete() -> None:
            try:
                settings.rel_path(rel).unlink()
            except FileNotFoundError as err:
                raise CommandError(ErrorCode.NOT_FOUND, f"{rel!r} not found") from err

        await commit_file_change(
            self._devices, [rel], _delete, message=f"Delete {rel}", precheck=_check
        )

    @api_command("files/rename")
    async def rename(
        self, *, path: Any, new_path: Any, skip: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        """
        Rename a package YAML within its folder and rewrite the ``!include`` references to it.

        Files in *skip* keep the old name. Returns the new ``path``, the files
        whose references were ``rewritten`` and the devices or archived files
        that may still name the old one (``unresolved``, e.g. substitution-built paths).
        """
        # Both ends must be safe unquoted: references to a name that needs
        # quoting can't be found or rewritten reliably.
        old = parse_file_path(path, in_subfolder=True, plain_name=True)
        new = parse_file_path(new_path, in_subfolder=True, plain_name=True)
        # A move would change what the file's own !secret / !include_dir_* resolve to.
        if posixpath.dirname(old) != posixpath.dirname(new):
            raise CommandError(ErrorCode.INVALID_ARGS, "a rename stays within the folder")
        if dependency_key(old) == dependency_key(new):
            raise CommandError(ErrorCode.INVALID_ARGS, "the new path is the current one")
        skipped = _parse_skip(skip)
        settings = self._db.settings
        users = self._users(dependency_key(old))
        # Planned here to know which files to lock, then again under the locks.
        plan = await run_in_executor(plan_rename, settings.config_dir, old, new, skipped)
        touched = sorted({old, new, *plan.texts})

        def _check() -> None:
            nonlocal plan
            if not settings.rel_path(old).is_file():
                raise CommandError(ErrorCode.NOT_FOUND, f"{old!r} not found")
            if settings.rel_path(new).exists():
                raise CommandError(ErrorCode.ALREADY_EXISTS, f"{new!r} already exists")
            plan = plan_rename(settings.config_dir, old, new, skipped)
            if not plan.texts.keys() <= set(touched):
                msg = "files changed while renaming; try again"
                raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)

        await commit_file_change(
            self._devices,
            touched,
            lambda: _apply_rename(settings, plan, old, new),
            message=f"Rename {old} to {new}",
            precheck=_check,
        )
        remaining = {dependency_key(new if rel == old else rel) for rel in plan.remaining}
        # Files that certainly still include the old one, and archived devices, whose
        # dependencies the scanner doesn't know, are reported directly.
        direct = {new if rel == old else rel for rel in plan.stale} | {
            rel for rel in plan.remaining if rel.startswith(f"{ARCHIVE_DIR}/")
        }
        return {
            "path": new,
            "rewritten": sorted(rel for rel in plan.texts if rel != old),
            "unresolved": sorted(
                direct
                | {
                    configuration
                    for configuration, deps in users.items()
                    if remaining & {dependency_key(configuration), *deps}
                }
            ),
        }

    @api_command("files/usage")
    async def usage(self, *, path: Any, **kwargs: Any) -> dict[str, Any]:
        """
        Return the devices whose config pulls in *path*, directly or through other includes.

        ``unknown`` lists devices whose files aren't known yet (never parsed, or
        still loading); ``referenced_by`` the files whose ``!include`` names
        *path* directly, packages and archived devices included, and
        ``rewritable`` those of them a rename updates.
        """
        rel = parse_file_path(path, in_subfolder=False)
        key = dependency_key(rel)
        referrers = await run_in_executor(find_referrers, self._db.settings.config_dir, rel)
        devices = self._devices.get_devices()
        return {
            "used_by": sorted(self._users(key)),
            "unknown": sorted(d.configuration for d in devices if d.dependencies is None),
            "referenced_by": referrers.referenced_by,
            "rewritable": referrers.rewritable,
        }

    @api_command("files/dependencies")
    async def dependencies(self, *, configuration: Any, **kwargs: Any) -> dict[str, Any]:
        """Return the files *configuration* pulls in, secrets files left out."""
        device = (
            self._devices.get_by_configuration(configuration)
            if isinstance(configuration, str)
            else None
        )
        if device is None:
            raise_device_not_found(str(configuration))
        deps = device.dependencies
        return {
            "files": [d for d in deps or () if not is_secrets_file(d)],
            "complete": deps is not None,
        }

    def _users(self, key: str) -> dict[str, tuple[str, ...]]:
        """Devices whose dependencies include *key*, mapped to those dependencies."""
        return {
            d.configuration: d.dependencies
            for d in self._devices.get_devices()
            if d.dependencies and key in d.dependencies
        }

    @property
    def _devices(self) -> DevicesController:
        devices = self._db.devices
        if devices is None:  # pragma: no cover — devices is always up post-start
            raise CommandError(ErrorCode.INTERNAL_ERROR, "devices controller unavailable")
        return devices


def _parse_skip(skip: Any) -> frozenset[str]:
    """Validate the wire ``skip`` list of config-relative paths."""
    if skip is None:
        return frozenset()
    if not isinstance(skip, list) or not all(isinstance(rel, str) for rel in skip):
        raise CommandError(ErrorCode.INVALID_ARGS, "skip must be a list of paths")
    return frozenset(skip)


def _apply_rename(settings: DashboardSettings, plan: RenamePlan, old: str, new: str) -> None:
    """Write *plan*'s referrers, then move *old* to *new*; put the texts back on failure."""
    written: list[str] = []
    try:
        for rel, text in plan.texts.items():
            write_user_yaml(settings.rel_path(rel), text)
            written.append(rel)
        settings.rel_path(old).rename(settings.rel_path(new))
    except BaseException as err:
        failed: list[str] = []
        for rel in written:
            try:
                write_user_yaml(settings.rel_path(rel), plan.originals[rel])
            except Exception:
                _LOGGER.exception("Couldn't restore %s after a failed rename", rel)
                failed.append(rel)
        if failed:
            msg = f"renaming failed and {', '.join(failed)} could not be restored"
            raise CommandError(ErrorCode.INTERNAL_ERROR, msg) from err
        raise
