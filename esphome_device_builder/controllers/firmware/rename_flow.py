"""Rename-chain orchestration: begin, address resolution, finalize swap, revert."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from esphome.helpers import write_file as atomic_write_file
from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.build_artifacts import wipe_device_build_dir
from ...helpers.device_yaml import resolved_device_name
from ...helpers.hostname import default_mdns_address
from ...helpers.storage_path import resolve_storage_path
from ...helpers.yaml import rewrite_rename_content
from ...models import ErrorCode, FirmwareJob, JobStatus, JobType
from . import factories

if TYPE_CHECKING:
    from pathlib import Path

    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)

# Shared with ``devices/rename``'s config-only path; both use the same
# rewriter, so both refusals steer the same way.
RENAME_REMEDY = (
    "Edit esphome.name to a plain value (or define the substitution "
    "in this file's substitutions: block) and try again."
)


async def begin_rename(
    controller: FirmwareController,
    *,
    configuration: str,
    new_name: str,
    content: str | None = None,
    new_content: str | None = None,
) -> tuple[FirmwareJob, FirmwareJob]:
    """
    Write the renamed YAML and enqueue its COMPILE + RENAME-tail chain.

    Returns ``(head, tail)``: a remote-eligible COMPILE of the new YAML
    plus the dependent flash-and-swap tail. *content* / *new_content*
    carry the old YAML's text and its rewrite when the caller already
    produced them (``devices/rename``); ``None`` derives them here.
    """
    new_filename = f"{new_name}.yaml"
    settings = controller._db.settings

    # ``rel_path`` resolves symlinks (blocking) — executor, like the runner.
    def _read() -> tuple[Path, str | None]:
        new_path = settings.rel_path(new_filename)
        if content is not None:
            return new_path, content
        try:
            return new_path, settings.rel_path(configuration).read_text(encoding="utf-8")
        except FileNotFoundError:
            return new_path, None

    new_path, content = await run_in_executor(_read)
    if content is None:
        raise CommandError(ErrorCode.INVALID_ARGS, f"Device {configuration} not found")

    if new_content is None:
        new_content = rewrite_rename_content(content, new_name, remedy=RENAME_REMEDY)
    port = await resolve_old_device_address(
        controller, configuration, resolved_device_name(content, configuration)
    )
    build_source = controller._resolve_install_source()

    async with controller.state.rename_fs_lock:
        head = factories.create_job(
            controller, new_filename, JobType.COMPILE, build_source=build_source
        )
        tail = factories.create_job(
            controller,
            configuration,
            JobType.RENAME,
            new_name=new_name,
            port=port,
            depends_on=head.job_id,
        )
        exclude = frozenset({head.job_id, tail.job_id})

        async def _stage() -> None:
            # Supersede only after the lock check passed — a rejected rename
            # must not cancel the device's in-flight build. The prior chain's
            # scheduled revert queues behind this lock, sees our active tail
            # owning the target, and skips; the write is an atomic overwrite,
            # not exclusive-create, because that superseded chain may have
            # left its own write behind.
            await controller._supersede_active_jobs(configuration, exclude_job_ids=set(exclude))
            await run_in_executor(atomic_write_file, new_path, new_content)

        await factories.commit_chain(
            controller,
            head,
            tail,
            supersede_configuration=new_filename,
            # The tail touches both names, so one check guards the chain.
            lock_job=tail,
            lock_exclude=exclude,
            stage=_stage,
        )
    return head, tail


def active_chain_owns_target(
    controller: FirmwareController, configuration: str, new_name: str
) -> bool:
    """Whether an active rename tail for *configuration* already targets *new_name*.

    A retry then passes the target-exists check: the on-disk file is the
    superseded chain's own write, not a foreign device's.
    """
    return any(
        job.is_rename_tail and job.configuration == configuration and job.new_name == new_name
        for job in controller.state.active_jobs()
    )


async def resolve_old_device_address(
    controller: FirmwareController, configuration: str, fallback_name: str
) -> str:
    """
    Return the OTA address a rename flashes (the pre-rename device).

    Priority: ``StorageJSON.address`` (the fused CLI's ``CORE.address``,
    honours ``wifi.use_address``), then scanner hostname / IP, then the
    mDNS default for *fallback_name*.
    """
    storage = await run_in_executor(lambda: StorageJSON.load(resolve_storage_path(configuration)))
    # Annotated hop: upstream ``StorageJSON`` is untyped, so ``.address`` is Any.
    stored_address: str | None = storage.address if storage is not None else None
    if stored_address:
        return stored_address
    devices = controller._db.devices
    if devices is not None:
        device = devices.get_by_configuration(configuration)
        if device is not None:
            if device.address:
                return device.address
            if device.ip:
                return device.ip
    return default_mdns_address(fallback_name)


async def finalize_rename_swap(controller: FirmwareController, job: FirmwareJob) -> None:
    """
    Drop the old YAML, StorageJSON, and build tree after the tail's flash succeeded.

    Failures log, never raise: the device already runs the renamed
    firmware, and failing here would revert-delete the matching YAML.
    """
    settings = controller._db.settings

    def _swap() -> None:
        # Build-tree wipe first — it resolves through the StorageJSON
        # the next line unlinks.
        wipe_device_build_dir(job.configuration)
        settings.rel_path(job.configuration).unlink(missing_ok=True)
        resolve_storage_path(job.configuration).unlink(missing_ok=True)

    try:
        await run_in_executor(_swap)
    except OSError:
        _LOGGER.warning(
            "Rename %s -> %s flashed but the old files could not be removed",
            job.configuration,
            job.new_name,
            exc_info=True,
        )


def on_job_terminal(controller: FirmwareController, job: FirmwareJob) -> None:
    """Schedule the rename revert for a tail that went terminal without completing.

    Every finalisation site calls this; a missed site strands the
    half-written new YAML on disk.
    """
    if not job.is_rename_tail or job.status is JobStatus.COMPLETED:
        return
    controller._db.create_background_task(revert_rename(controller, job))


async def revert_rename(controller: FirmwareController, job: FirmwareJob) -> None:
    """Remove the failed/cancelled chain's new YAML unless a newer chain owns it."""
    new_filename = job.new_filename
    async with controller.state.rename_fs_lock:
        for other in controller.state.active_jobs():
            if other.job_id == job.job_id:
                continue
            if other.is_rename_tail and other.new_name == job.new_name:
                return
        settings = controller._db.settings

        def _cleanup() -> None:
            # The head compile may have created the new name's build tree +
            # StorageJSON; drop them with the YAML so nothing is orphaned.
            wipe_device_build_dir(new_filename)
            resolve_storage_path(new_filename).unlink(missing_ok=True)
            settings.rel_path(new_filename).unlink(missing_ok=True)

        await run_in_executor(_cleanup)
    _LOGGER.info("Rename of %s reverted; removed %s", job.configuration, new_filename)
    devices = controller._db.devices
    if devices is not None:
        await devices.reload_configuration(new_filename)
