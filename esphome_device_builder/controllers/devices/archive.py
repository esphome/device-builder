"""
Archive / delete filesystem helpers for the devices controller.

Soft-delete (``archive``) moves the YAML into
``<config_dir>/archive/``, wipes the build dir + StorageJSON
sidecar, and clears volatile metadata; ``unarchive`` is the
inverse move; ``delete`` is the no-going-back variant that
removes the YAML and every sidecar keyed on its filename.
``list_archived_sync`` reads the archive dir for the
"Show archived devices" toggle. ``run_bulk_per_device`` is the
shared dispatcher behind ``delete_bulk`` / ``archive_bulk`` —
one scan at the end instead of N.

The controller keeps thin ``_archive_single`` /
``_unarchive_single`` / ``_list_archived_sync`` /
``_delete_archived_single`` / ``_delete_single`` /
``_run_bulk_per_device`` delegates so existing tests can keep
calling ``controller._archive_single(...)`` etc. while the
filesystem dance lives here.
"""

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
    _archive_clear_device_sidecars,
    _remove_device_sidecars,
    _wipe_device_build_dir,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


async def archive_single(controller: DevicesController, configuration: str) -> None:
    """Soft-delete: move the YAML into ``<config_dir>/archive/`` and wipe build artifacts.

    Mirrors the legacy dashboard's archive flow with one
    deliberate divergence: we also wipe the StorageJSON
    sidecar (a pure build artifact — ``firmware_bin_path`` /
    ``loaded_integrations`` / ``target_platform`` go stale
    the moment the build dir is removed). The legacy dashboard
    preserved StorageJSON so unarchive could restore cached
    IP / version, but ours uses ``ext_storage_path`` which
    is per-filename keyed — a future same-name configuration
    would inherit the archived device's stale build state
    until recompiled. Wiping on archive trades a few seconds
    of "unknown state" after unarchive (the scanner + monitor
    refill from the next mDNS broadcast + the next compile)
    for full isolation against same-name new devices.

    The device-metadata sidecar is treated more carefully —
    only volatile fields (``ip``, ``expected_config_hash``)
    are cleared. Stable identity fields (``board_id``,
    ``friendly_name``, ``comment``) survive so an unarchive
    of the same YAML restores the user-visible state
    unchanged. ``board_id`` in particular is the catalog →
    YAML match key; an earlier iteration wiped the entire
    entry and forced a re-derive on every archive →
    unarchive cycle. See
    ``_archive_clear_device_sidecars`` for the keep / clear
    rationale.

    Build dir wipe matches what ``delete_single`` does — an
    archived device's compile output is dead weight (the
    user can recompile after unarchive). The YAML itself
    stays on disk so the operation is reversible.
    """
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
            # Same name already archived. We can't silently rename
            # to ``<name> (2).yaml`` because the StorageJSON sidecar
            # and metadata stay keyed on the original filename —
            # a later unarchive of the suffixed copy would surface
            # without its sidecar and lose the cached address /
            # version / loaded_integrations. Refuse the operation
            # and let the user resolve the collision explicitly
            # (unarchive the existing copy or delete it).
            msg = (
                f"Cannot archive {configuration}: an archived config "
                "with the same name already exists. Unarchive or "
                "permanently delete the existing archive first."
            )
            raise FileExistsError(msg)
        # Wipe build dir first (same shape as delete), then
        # move the YAML, then clear the build-artifact
        # sidecars while keeping stable identity fields so an
        # unarchive of this same YAML restores its
        # user-visible state. See the docstring for the
        # keep / clear split.
        _wipe_device_build_dir(configuration)
        shutil.move(str(config_path), str(target))
        _archive_clear_device_sidecars(config_dir, configuration)

    try:
        await loop.run_in_executor(None, _archive_sync)
    except FileExistsError as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc


async def unarchive_single(controller: DevicesController, configuration: str) -> None:
    """Move an archived YAML back into the active config_dir.

    Refuses to clobber an existing active YAML — that case
    means the user already created a new device under the same
    filename, and silently overwriting it would surprise them.
    Surface a ``CommandError`` instead so the dialog can prompt
    for a different action.
    """
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
    """Read ``<config_dir>/archive/`` and parse each YAML's meta block.

    Returns one dict per archived YAML with the same name /
    friendly_name / comment fields the active device list
    carries, plus ``configuration`` so the dashboard can
    address each entry. Files that don't parse are skipped
    with a debug log — the archive dir is user-managed and
    a stray non-YAML file shouldn't crash the listing.

    When the YAML's ``esphome:`` block is sparse (e.g. friendly
    name only ever lived in StorageJSON because the user wrote
    it via the dashboard's edit dialog rather than the YAML),
    fall back to the StorageJSON sidecar before degrading to
    the bare filename. ``archive_single`` wipes its own
    sidecars on archive, so the fallback only matters for
    legacy archives (created by the upstream ESPHome dashboard
    or by an earlier version of this server before the sidecar
    wipe landed) and for entries dropped into the archive dir
    externally.
    """
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
    """Permanently remove an archived YAML and its sidecars.

    Mirrors ``delete_single`` but operates on
    ``<config_dir>/archive/<configuration>`` instead of the
    active config_dir. The build dir is already gone (archive
    wipes it), so this only has to remove the YAML, the
    StorageJSON sidecar, and the device-metadata sidecar.

    Defense-in-depth: the StorageJSON / metadata sidecars are
    keyed on the bare filename, so if an active config of the
    same name has been re-created since the archive, those
    sidecars belong to the live device and removing them
    would wipe its cached IP / hash / loaded_integrations.
    ``archive_single`` already wipes its own sidecars on the
    way in (so this collision shouldn't happen in practice),
    but we still guard with an existence check on the active
    path. Callers expect best-effort cleanup of orphan
    sidecars, not a guarantee of their removal.
    """
    loop = asyncio.get_running_loop()
    config_dir = controller._db.settings.config_dir
    archive_path = config_dir / "archive" / configuration
    active_path = controller._db.settings.rel_path(configuration)

    def _delete_all() -> None:
        if not archive_path.exists():
            msg = f"Archived file not found: {configuration}"
            raise FileNotFoundError(msg)
        archive_path.unlink()
        if active_path.exists():
            # An active config with the same filename owns the
            # sidecars now — leave them alone.
            return
        _remove_device_sidecars(config_dir, configuration)

    await loop.run_in_executor(None, _delete_all)


async def delete_single(controller: DevicesController, configuration: str) -> None:
    """Delete a single device and all associated files."""
    config_path = controller._db.settings.rel_path(configuration)
    loop = asyncio.get_running_loop()
    config_dir = controller._db.settings.config_dir

    def _delete_all() -> None:
        # Existence check runs in the executor too — ``Path.exists``
        # stat()s the filesystem and would block the event loop if
        # called from the async caller.
        if not config_path.exists():
            msg = f"File not found: {configuration}"
            raise FileNotFoundError(msg)
        # Wipe build dir first so a partial failure later still
        # leaves the user able to retry the delete.
        _wipe_device_build_dir(configuration)
        config_path.unlink(missing_ok=True)
        (config_dir / ".trash" / configuration).unlink(missing_ok=True)
        (config_dir / ".archive" / f"{configuration}.json").unlink(missing_ok=True)
        _remove_device_sidecars(config_dir, configuration)

    await loop.run_in_executor(None, _delete_all)


async def run_bulk_per_device(
    controller: DevicesController,
    configurations: list[str],
    action: Callable[[str], Awaitable[None]],
) -> list[dict[str, Any]]:
    """Run *action* per configuration; return one result dict each.

    Shared shape behind ``delete_bulk`` and ``archive_bulk``: each
    item in the returned list is ``{configuration, success}`` plus
    ``error`` (the exception's ``str``) on failure. A single
    ``_scanner.scan()`` runs after the whole batch — bulk teardown
    otherwise N-squares the bus traffic the dashboard subscribes to.
    """
    results: list[dict[str, Any]] = []
    for configuration in configurations:
        try:
            await action(configuration)
            results.append({"configuration": configuration, "success": True})
        except Exception as exc:
            results.append(
                {
                    "configuration": configuration,
                    "success": False,
                    "error": str(exc),
                }
            )
    await controller._scanner.scan()
    return results
