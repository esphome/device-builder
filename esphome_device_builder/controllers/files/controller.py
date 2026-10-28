"""Files controller — package / include YAMLs that are not devices."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...constants import is_secrets_file
from ...helpers.api import CommandError, api_command
from ...helpers.async_ import run_in_executor
from ...helpers.atomic_io import atomic_write_exclusive
from ...helpers.device_config import raise_device_not_found
from ...models import ErrorCode
from ..devices.helpers import refuse_empty_write
from .paths import dependency_key, list_package_files, parse_file_path

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder


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
        rel = parse_file_path(path, in_subfolder=True)
        if not isinstance(content, str):
            raise CommandError(ErrorCode.INVALID_ARGS, "content must be a string")
        if not content.strip():
            refuse_empty_write(rel)
        settings = self._db.settings

        def _create() -> None:
            # rel_path resolves symlinks (blocking), so it runs here, not on the loop.
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

        await self._db.devices.commit_file_change(rel, _create, message=f"Create {rel}")
        return {"path": rel}

    @api_command("files/delete")
    async def delete(self, *, path: Any, **kwargs: Any) -> None:
        """Delete a package YAML."""
        rel = parse_file_path(path, in_subfolder=True)
        settings = self._db.settings

        def _delete() -> None:
            try:
                settings.rel_path(rel).unlink()
            except FileNotFoundError as err:
                raise CommandError(ErrorCode.NOT_FOUND, f"{rel!r} not found") from err

        await self._db.devices.commit_file_change(rel, _delete, message=f"Delete {rel}")

    @api_command("files/usage")
    async def usage(self, *, path: Any, **kwargs: Any) -> dict[str, Any]:
        """
        Return the devices whose config pulls in *path*, directly or through other includes.

        ``unknown`` lists devices whose files aren't known yet (never parsed, or still loading).
        """
        key = dependency_key(parse_file_path(path, in_subfolder=False))
        devices = self._db.devices.get_devices()
        return {
            "used_by": sorted(
                d.configuration for d in devices if d.dependencies and key in d.dependencies
            ),
            "unknown": sorted(d.configuration for d in devices if d.dependencies is None),
        }

    @api_command("files/dependencies")
    async def dependencies(self, *, configuration: Any, **kwargs: Any) -> dict[str, Any]:
        """Return the files *configuration* pulls in, secrets files left out."""
        device = (
            self._db.devices.get_by_configuration(configuration)
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
