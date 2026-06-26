"""Firmware-job → device-state sync helpers."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from esphome.storage_json import StorageJSON

from ...helpers.config_hash import compute_yaml_config_hash
from ...helpers.event_bus import Event
from ...helpers.remote_build_layout import (
    parse_from_configuration as parse_remote_build_path,
)
from ...helpers.storage_path import resolve_storage_path
from ...models import JobLifecycleData, JobStatus, JobType

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)

# Delay before the post-flash Native-API version re-probe so the device
# has time to reboot into the new image before we connect.
_POST_FLASH_VERSION_REPROBE_DELAY = 60


def on_job_completed(controller: DevicesController, event: Event[JobLifecycleData]) -> None:
    """
    Refresh a device's cached state after a successful firmware job.

    Without this hook, a freshly-flashed device keeps its stale
    ``has_pending_changes=True`` (the still-orange "update
    pending" dot) since the disk scanner only re-evaluates on
    YAML stat change.

    COMPILE / INSTALL also recompute ``expected_config_hash``;
    UPLOAD reuses the prior compile's.
    """
    job = event.data["job"]
    if job.status != JobStatus.COMPLETED:
        return
    job_type = job.job_type
    if job_type == JobType.RENAME:
        # ``esphome rename`` deletes the old YAML and writes a
        # new one with a different filename; full scan is the
        # simplest way to pick up both transitions. First migrate
        # the device's filename-keyed metadata (labels / comment /
        # board_id live in the sidecar) so the scan rebuilds it
        # under the new name instead of starting fresh and dropping
        # the user's labels.
        new_name = job.new_name
        old_configuration = job.configuration
        if new_name and old_configuration:
            # ``new_name`` is a bare stem today; strip a stray
            # extension defensively so the key can't become
            # ``livingroom.yaml.yaml`` and target the wrong entry.
            new_configuration = f"{Path(new_name).stem}.yaml"
            controller._db.create_background_task(
                migrate_metadata_then_scan(controller, old_configuration, new_configuration)
            )
        else:
            controller._db.create_background_task(controller._scanner.scan())
        return
    configuration = job.configuration
    if not configuration:
        return
    if parse_remote_build_path(configuration) is not None:
        # Receiver-side remote-build job; the YAML belongs to
        # a paired offloader, not this dashboard.
        return
    if job_type == JobType.CLEAN:
        # ``esphome clean`` wipes the build tree; the
        # build-size cache is now stale and the worker's
        # pair-equality short-circuit clears the cached triple
        # so the drawer / table flip back to the placeholder.
        controller._build_size.request(configuration)
        return
    if job_type not in (JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL):
        return
    recompute_hash = job_type in (JobType.COMPILE, JobType.INSTALL)
    flashed = job_type in (JobType.UPLOAD, JobType.INSTALL)
    # Routed through the controller's bound delegate so tests
    # that monkeypatch ``_refresh_after_firmware_job`` on the
    # instance still intercept.
    controller._db.create_background_task(
        controller._refresh_after_firmware_job(
            configuration, recompute_hash=recompute_hash, flashed=flashed
        )
    )


async def refresh_after_job(
    controller: DevicesController,
    configuration: str,
    *,
    recompute_hash: bool,
    flashed: bool,
) -> None:
    """
    Persist the YAML's freshly-compiled hash and reload the device.

    Always reloads after the optional hash recompute so the
    mtime side of ``has_pending_changes`` flips. When *flashed*,
    optimistically pins ``deployed_config_hash`` + ``deployed_version``
    so the dot and the "update available" badge clear immediately
    rather than waiting on the rebooted device's mDNS announce, then
    schedules a delayed Native-API re-probe to confirm the running
    version (the only signal of a rollback when mDNS can't reach us).
    """
    if recompute_hash:
        await controller._persist_expected_config_hash(configuration)
    await controller._scanner.reload(configuration)
    if flashed:
        await controller._sync_deployed_state_after_flash(configuration)
        controller._schedule_version_reprobe(configuration)
    # A real compile moves the build-size cache's freshness
    # pair (build-dir mtime + ``build_info.json`` mtime); the
    # worker short-circuits when the pair didn't actually move
    # (e.g. UPLOAD-only).
    controller._build_size.request(configuration)


async def persist_expected_config_hash(controller: DevicesController, configuration: str) -> None:
    """
    Read the canonical config_hash from build_info.json and persist it.

    Read rather than recompute: ``CORE.config_hash`` is
    sensitive to post-codegen state (id-pinning, default
    backfill, normalisation) that ``read_config`` alone doesn't
    apply, so reproducing the build's hash in-process is
    fragile (verified against ``acfloatmonitor32.yaml``:
    pre-codegen ``f3e21d5a`` vs firmware-baked ``5a94a12d``).
    Logs a warning rather than failing on a missing or
    malformed ``build_info.json`` so an upstream ESPHome
    shape change surfaces visibly.
    """
    yaml_path = controller._db.settings.rel_path(configuration)
    new_hash = await compute_yaml_config_hash(yaml_path)
    if not new_hash:
        _LOGGER.warning(
            "Could not read config_hash from build_info.json for %s; "
            "the drawer's Local hash may stay stale until the next "
            "flash. If this persists across compiles, check that "
            "ESPHome's build_info.json schema hasn't changed.",
            configuration,
        )
        return
    controller._metadata_store.update(configuration, expected_config_hash=new_hash)
    _LOGGER.debug("Stored expected_config_hash for %s: %s", configuration, new_hash)


async def sync_deployed_state_after_flash(
    controller: DevicesController, configuration: str
) -> None:
    """
    Optimistically align ``deployed_config_hash`` + ``deployed_version`` with the flash.

    A successful flash means the freshly-compiled binary is on the
    device, so its ``expected_config_hash`` and
    ``StorageJSON.esphome_version`` describe what the device now runs.
    Driving both through ``apply_config_hash`` / ``apply_version`` lets
    the existing ``_on_*_change`` callbacks write the fields, fire
    ``DEVICE_UPDATED``, and seed the monitor's cache so the rebooted
    device's matching announce deduplicates. Clears the orange dot and
    the "update available" badge without waiting on an mDNS announce —
    one that never arrives in mDNS-dark deployments (Docker-bridge).
    """
    device = controller._scanner.get_by_configuration(configuration)
    if device is None:
        return
    if device.expected_config_hash:
        controller._state_monitor.apply_config_hash(device.name, device.expected_config_hash)
    version = await asyncio.to_thread(_read_compiled_esphome_version, configuration)
    if version:
        controller._state_monitor.apply_version(device.name, version)


def schedule_version_reprobe(controller: DevicesController, configuration: str) -> None:
    """
    Arm a one-shot Native-API version re-probe ~60s after a flash.

    The delay lets the device reboot into the new image before we
    connect; the re-probe then confirms the optimistically-pinned
    version (and catches a rollback) where mDNS can't reach us.
    Re-arming for the same configuration cancels the prior timer so a
    rapid re-flash doesn't stack probes; the handle is tracked on the
    controller so ``stop`` can cancel anything still pending.
    """
    existing = controller._reprobe_timers.pop(configuration, None)
    if existing is not None:
        existing.cancel()
    loop = asyncio.get_running_loop()
    controller._reprobe_timers[configuration] = loop.call_later(
        _POST_FLASH_VERSION_REPROBE_DELAY, _fire_version_reprobe, controller, configuration
    )


async def migrate_metadata_then_scan(
    controller: DevicesController, old_configuration: str, new_configuration: str
) -> None:
    """Move the renamed device's metadata before the scan rebuilds it."""
    try:
        await controller._migrate_device_metadata(old_configuration, new_configuration)
    except Exception:
        # A migration failure must not skip the scan — the renamed
        # device's ONLINE/OFFLINE transitions still need picking up.
        _LOGGER.exception(
            "Failed to migrate metadata from %s to %s on rename; scanning anyway",
            old_configuration,
            new_configuration,
        )
    await controller._scanner.scan()


def _fire_version_reprobe(controller: DevicesController, configuration: str) -> None:
    """
    Timer callback: ask the monitor to verify the device's running version.

    A no-op if the device vanished in the interim. The request still
    honours the monitor's ``priority_for != MDNS`` guard, so a device
    already seen over mDNS by now is skipped rather than probed.
    """
    controller._reprobe_timers.pop(configuration, None)
    device = controller._scanner.get_by_configuration(configuration)
    if device is not None:
        controller._state_monitor.request_version_reprobe(device.name)


def _read_compiled_esphome_version(configuration: str) -> str:
    """Read ``esphome_version`` from the device's StorageJSON; ``""`` on miss."""
    storage = StorageJSON.load(resolve_storage_path(configuration))
    if storage is None or not storage.esphome_version:
        return ""
    return str(storage.esphome_version)
