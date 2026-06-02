"""Archive / delete filesystem helpers for the devices controller."""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import TYPE_CHECKING, Any

from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError
from ...helpers.device_yaml import parse_esphome_meta
from ...helpers.storage_path import resolve_storage_path
from ...models import ErrorCode
from .helpers import (
    _unlink_compiled_config,
    _unlink_storage_sidecar,
    _wipe_device_build_dir,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


async def archive_single(controller: DevicesController, configuration: str) -> None:
    """Soft-delete: move the YAML into ``<config_dir>/archive/`` and wipe build artifacts."""
    config_path = controller._db.settings.rel_path(configuration)
    loop = asyncio.get_running_loop()
    config_dir = controller._db.settings.config_dir

    def _archive_sync() -> None:
        if not config_path.exists():
            msg = f"File not found: {configuration}"
            raise FileNotFoundError(msg)
        archive_dir = config_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        target = archive_dir / configuration
        if target.exists():
            # Refuse rather than auto-rename; the StorageJSON sidecar
            # is filename-keyed, so unarchiving a ``<name> (2).yaml``
            # later would lose the cached state.
            msg = (
                f"Cannot archive {configuration}: an archived config "
                "with the same name already exists. Unarchive or "
                "permanently delete the existing archive first."
            )
            raise FileExistsError(msg)
        # Wipe build dir + StorageJSON first; deliberate divergence
        # from the upstream dashboard. Our ``ext_storage_path`` is
        # per-filename keyed, so a future same-name device would
        # otherwise inherit the archived device's stale
        # firmware_bin_path / loaded_integrations / target_platform.
        _wipe_device_build_dir(configuration)
        shutil.move(str(config_path), str(target))
        _unlink_storage_sidecar(configuration)
        _unlink_compiled_config(configuration)

    try:
        await loop.run_in_executor(None, _archive_sync)
    except FileExistsError as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc
    # Drop volatile fields across both stores: live mDNS state and
    # build-dir caches in the data_dir store, plus ``mac_address``
    # in the shared sidecar (intrinsic to the physical board, but
    # volatile across YAML → board re-bindings on unarchive).
    # Identity fields (board_id / friendly_name / comment / labels)
    # survive so unarchive restores user-visible state.
    await controller._clear_volatile_device_metadata(configuration)


async def unarchive_single(controller: DevicesController, configuration: str) -> None:
    """Move an archived YAML back into the active config_dir; refuse on filename clash."""
    loop = asyncio.get_running_loop()
    config_dir = controller._db.settings.config_dir
    archive_path = config_dir / "archive" / configuration
    target = controller._db.settings.rel_path(configuration)

    def _unarchive_sync() -> None:
        if not archive_path.exists():
            msg = f"Archived file not found: {configuration}"
            raise FileNotFoundError(msg)
        if target.exists():
            msg = (
                f"Cannot unarchive {configuration}: an active config "
                f"with the same name already exists"
            )
            raise FileExistsError(msg)
        shutil.move(str(archive_path), str(target))

    try:
        await loop.run_in_executor(None, _unarchive_sync)
    except FileExistsError as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc


def list_archived_sync(controller: DevicesController) -> list[dict[str, Any]]:
    """Read ``<config_dir>/archive/`` and parse each YAML's meta block."""
    archive_dir = controller._db.settings.config_dir / "archive"
    if not archive_dir.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(archive_dir.iterdir()):
        if path.suffix not in (".yaml", ".yml") or path.name.startswith("."):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            _LOGGER.debug("Failed to read archived YAML %s", path, exc_info=True)
            continue
        name, friendly_name, comment, _ = parse_esphome_meta(content)
        if not name or not friendly_name or comment is None:
            # Sparse ``esphome:`` block; fall back to StorageJSON so legacy
            # archives (and externally-dropped files) still surface a label.
            storage = StorageJSON.load(resolve_storage_path(path.name))
            if storage is not None:
                name = name or storage.name
                friendly_name = friendly_name or storage.friendly_name
                if comment is None:
                    comment = storage.comment
        results.append(
            {
                "configuration": path.name,
                "name": name or path.stem,
                "friendly_name": friendly_name or name or path.stem,
                "comment": comment,
            }
        )
    return results


async def delete_archived_single(controller: DevicesController, configuration: str) -> None:
    """Permanently remove an archived YAML and its sidecars."""
    loop = asyncio.get_running_loop()
    config_dir = controller._db.settings.config_dir
    archive_path = config_dir / "archive" / configuration
    active_path = controller._db.settings.rel_path(configuration)

    def _delete_all() -> bool:
        if not archive_path.exists():
            msg = f"Archived file not found: {configuration}"
            raise FileNotFoundError(msg)
        archive_path.unlink()
        if active_path.exists():
            # An active config with the same filename owns the
            # sidecars now; leave them alone.
            return False
        _unlink_storage_sidecar(configuration)
        _unlink_compiled_config(configuration)
        return True

    sidecars_purged = await loop.run_in_executor(None, _delete_all)
    if sidecars_purged:
        # Drop the per-device metadata entry (both the store +
        # shared identity sidecar) on the event loop side and
        # flush immediately; a quick restart after the delete
        # mustn't resurrect a stale entry.
        await controller._delete_device_metadata(configuration)


async def delete_single(controller: DevicesController, configuration: str) -> None:
    """Delete a single device and all associated files."""
    config_path = controller._db.settings.rel_path(configuration)
    loop = asyncio.get_running_loop()
    config_dir = controller._db.settings.config_dir

    def _delete_all() -> None:
        # Existence check stays inside the executor; Path.exists
        # performs a filesystem stat and would block the event
        # loop otherwise.
        if not config_path.exists():
            msg = f"File not found: {configuration}"
            raise FileNotFoundError(msg)
        # Wipe build dir first so a partial failure later leaves
        # the user able to retry the delete.
        _wipe_device_build_dir(configuration)
        config_path.unlink(missing_ok=True)
        (config_dir / ".trash" / configuration).unlink(missing_ok=True)
        (config_dir / ".archive" / f"{configuration}.json").unlink(missing_ok=True)
        _unlink_storage_sidecar(configuration)
        _unlink_compiled_config(configuration)

    await loop.run_in_executor(None, _delete_all)
    await controller._delete_device_metadata(configuration)


async def run_bulk_per_device(
    controller: DevicesController,
    configurations: list[str],
    action: Callable[[str], Awaitable[None]],
) -> list[dict[str, Any]]:
    """Run *action* per configuration; one ``{configuration, success, error?}`` dict each."""
    return await run_bulk_per_row(controller, configurations, action, lambda c: c)


async def run_bulk_per_row[T](
    controller: DevicesController,
    rows: Sequence[T],
    action: Callable[[T], Awaitable[None]],
    get_configuration: Callable[[T], str],
) -> list[dict[str, Any]]:
    """Run *action* per row; one result row per input row in input order.

    Use when each row carries payload beyond a bare configuration
    string. Duplicate configurations produce duplicate result rows;
    last-write-wins on disk. ``get_configuration`` is called on
    failures too, so it must tolerate malformed rows — return
    ``""`` for "couldn't extract".
    """
    results: list[dict[str, Any]] = []
    for row in rows:
        configuration = get_configuration(row)
        try:
            await action(row)
            results.append({"configuration": configuration, "success": True})
        except Exception as exc:  # noqa: BLE001 — batch op: per-row error captured into the result row
            results.append(
                {
                    "configuration": configuration,
                    "success": False,
                    "error": str(exc),
                }
            )
    await controller._scanner.scan()
    return results
