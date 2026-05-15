"""Firmware-job → device-state sync helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...helpers.config_hash import compute_yaml_config_hash
from ...helpers.event_bus import Event
from ...helpers.remote_build_layout import (
    parse_from_configuration as parse_remote_build_path,
)
from ...models import JobLifecycleData, JobStatus, JobType

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


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
        # simplest way to pick up both transitions.
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
    optimistically pins ``deployed_config_hash = expected`` so
    the dot clears immediately rather than waiting on the
    rebooted device's mDNS announce; if the OTA actually failed
    silently, ``_on_config_hash_change`` pushes the real value
    back on the next announce.
    """
    if recompute_hash:
        await controller._persist_expected_config_hash(configuration)
    await controller._scanner.reload(configuration)
    if flashed:
        controller._sync_deployed_hash_after_flash(configuration)
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


def sync_deployed_hash_after_flash(controller: DevicesController, configuration: str) -> None:
    """
    Optimistically align ``deployed_config_hash`` with the just-flashed image.

    Driving the update through ``apply_config_hash`` lets the
    existing ``_on_config_hash_change`` callback handle the
    device-field write + ``DEVICE_UPDATED`` event, and seeds
    the monitor's per-name cache so the rebooted device's
    matching announce deduplicates instead of firing a
    redundant event.
    """
    device = next(
        (d for d in controller._scanner.devices if d.configuration == configuration),
        None,
    )
    if device is None or not device.expected_config_hash:
        return
    controller._state_monitor.apply_config_hash(device.name, device.expected_config_hash)
