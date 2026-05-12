"""
Firmware-job → device-state sync helpers.

Subscribed to the firmware queue's ``JOB_COMPLETED`` event by the
controller. After a successful COMPILE / UPLOAD / INSTALL,
recomputes the YAML's ``expected_config_hash`` from
``build_info.json`` and optimistically pins
``deployed_config_hash`` to the freshly-flashed value so the
"update pending" dot clears immediately rather than waiting on
the rebooted device's next mDNS announce. RENAME triggers a full
scan; CLEAN just nudges the build-size cache.

The controller keeps thin bound-method delegates
(``_on_firmware_job_completed``, ``_refresh_after_firmware_job``,
``_persist_expected_config_hash``, ``_sync_deployed_hash_after_flash``,
``_persist_storage_version_async``) so:

* WS callbacks see the methods at their original names.
* Tests can monkeypatch ``_refresh_after_firmware_job`` /
  ``_persist_storage_version_async`` on the instance and the
  patched callable is what runs (the dispatcher here calls
  ``controller._refresh_after_firmware_job(...)``, not the
  module function directly).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ...helpers.config_hash import compute_yaml_config_hash
from ...helpers.event_bus import Event
from ...models import JobLifecycleData, JobStatus, JobType

if TYPE_CHECKING:
    from .controller import DevicesController

_LOGGER = logging.getLogger(__name__)


def on_job_completed(controller: DevicesController, event: Event[JobLifecycleData]) -> None:
    """
    Refresh a device's cached state after a successful firmware job.

    Without this hook, a freshly-flashed device keeps its stale
    ``has_pending_changes=True`` — the symptom users see as a
    still-orange "update pending" dot — because the disk scanner
    only re-evaluates when the YAML file's stat changes.

    COMPILE and INSTALL also recompute the YAML's
    ``expected_config_hash`` here so the next mDNS resolve can
    compare against the firmware's broadcast hash; UPLOAD doesn't
    recompile, so it reuses whatever the previous compile cached.
    """
    job = event.data["job"]
    if job.status != JobStatus.COMPLETED:
        return
    job_type = job.job_type
    if job_type == JobType.RENAME:
        # ``esphome rename`` deletes the old YAML and writes a new
        # one with a different filename — neither path is the
        # ``configuration`` field on the job. A full scan is the
        # simplest way to pick up both the disappearance of the
        # old entry and the appearance of the new one.
        controller._db.create_background_task(controller._scanner.scan())
        return
    configuration = job.configuration
    if not configuration:
        return
    if job_type == JobType.CLEAN:
        # ``esphome clean`` removes the per-device build tree;
        # the build-size cache for this device is now stale
        # (cached non-zero, current dir mtime → 0). The pair-
        # equality short-circuit in
        # ``refresh_build_size_if_stale`` detects that and
        # walks once to clear the cached triple, so the drawer
        # / table flip back to the em-dash placeholder. No
        # hash recompute / flash bookkeeping needed for CLEAN.
        controller._build_size.request(configuration)
        return
    if job_type not in (JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL):
        return
    recompute_hash = job_type in (JobType.COMPILE, JobType.INSTALL)
    flashed = job_type in (JobType.UPLOAD, JobType.INSTALL)
    # Routed through the controller's bound delegate so tests that
    # monkeypatch ``_refresh_after_firmware_job`` on the instance
    # still intercept the call.
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

    When *recompute_hash* is True, recomputes the YAML's
    ``CORE.config_hash`` and writes it to the metadata sidecar so
    the next mDNS resolve can compare against the firmware's
    broadcast. The device is always reloaded afterwards — even
    when hash computation is skipped or fails — so the mtime side
    of ``has_pending_changes`` still flips after a successful
    compile.

    When *flashed* is True (UPLOAD or INSTALL completed), the
    firmware on the device was just replaced with the binary that
    compiled to ``expected_config_hash``. The reloaded device
    otherwise keeps the *previous* mDNS-cached
    ``deployed_config_hash`` — usually a now-stale value — so the
    hash comparison reads ``expected != deployed`` and the dot
    stays orange until the rebooted device's mDNS announce
    propagates. That can be many seconds, sometimes longer if the
    device's network announce gets dropped, and the user sees a
    successful flash with a still-orange dot. Optimistically pin
    deployed = expected on the reloaded device and recompute the
    flag so the dot clears immediately. mDNS still gets to
    correct the hash later — if the new firmware advertises a
    different hash (e.g. because the OTA actually failed and the
    device kept the old image), ``_on_config_hash_change`` will
    push the real value back in.
    """
    if recompute_hash:
        await controller._persist_expected_config_hash(configuration)
    await controller._scanner.reload(configuration)
    if flashed:
        controller._sync_deployed_hash_after_flash(configuration)
    # A real compile moves the freshness pair the build-size
    # cache keys off (build-dir mtime + ``build_info.json``
    # mtime); hand off to the build-size worker so the drawer
    # / table show an up-to-date "Build size" value the next
    # time the frontend reads the device list. The worker
    # short-circuits when the pair didn't actually move (e.g.
    # an UPLOAD-only job that didn't recompile).
    controller._build_size.request(configuration)


async def persist_expected_config_hash(controller: DevicesController, configuration: str) -> None:
    """
    Read the canonical config_hash from build_info.json and persist it.

    ESPHome's build (and ``--only-generate``) writes the
    ``config_hash`` to ``build_info.json`` after running the full
    validate + codegen pipeline. We read that value back rather
    than recompute it, because reproducing the build's hash
    in-process is fragile — ``CORE.config_hash`` is sensitive to
    post-codegen state (id-pinning, default backfill,
    normalisation) that ``read_config`` alone doesn't apply.
    Verified against ``acfloatmonitor32.yaml``: pre-codegen yields
    ``f3e21d5a`` while the firmware bakes in ``5a94a12d``.

    No-op when the hash can't be read. The caller is on the
    post-build / post-only-generate path, so a missing or
    malformed ``build_info.json`` here is unexpected — log a
    warning so an upstream ESPHome shape change doesn't
    silently leave the sidecar out of date.
    ``compute_has_pending_changes`` will lean on the bin mtime
    in that gap, which catches the "user just edited the YAML"
    case but won't notice firmware that's drifted from the
    compile (e.g. flashed elsewhere) — the dot can read
    in-sync when it shouldn't until the next real flash
    rewrites the sidecar.
    """
    yaml_path = controller._db.settings.rel_path(configuration)
    new_hash = await compute_yaml_config_hash(yaml_path)
    if not new_hash:
        _LOGGER.warning(
            "Could not read config_hash from build_info.json for %s — "
            "the drawer's Local hash may stay stale until the next flash. "
            "If this persists across compiles, check that ESPHome's "
            "build_info.json schema hasn't changed.",
            configuration,
        )
        return
    await controller._persist_device_metadata_async(configuration, expected_config_hash=new_hash)
    _LOGGER.debug("Stored expected_config_hash for %s: %s", configuration, new_hash)


def sync_deployed_hash_after_flash(controller: DevicesController, configuration: str) -> None:
    """
    Optimistically align ``deployed_config_hash`` with the just-flashed image.

    See :func:`refresh_after_job` for the rationale. Driving the
    update through ``apply_config_hash`` lets the existing
    ``_on_config_hash_change`` callback handle the device-field
    write + ``DEVICE_UPDATED`` event, so the post-flash sync
    follows the same code path as a real mDNS announce.
    ``apply_config_hash`` also seeds the monitor's per-name cache,
    so when the rebooted device's announce lands with the *same*
    hash the de-dup short-circuits and we don't fire a redundant
    event.
    """
    device = next(
        (d for d in controller._scanner.devices if d.configuration == configuration),
        None,
    )
    if device is None or not device.expected_config_hash:
        return
    controller._state_monitor.apply_config_hash(device.name, device.expected_config_hash)


async def persist_storage_version_async(
    controller: DevicesController, configuration: str, version: str
) -> None:
    """Update ``StorageJSON.esphome_version`` on disk if it differs.

    The sync write is dispatched to a thread so the controller's
    event loop isn't blocked on disk I/O. The controller's
    ``_persist_storage_version`` static method is the actual
    writer — kept as a static on the class so tests can call it
    via ``DevicesController._persist_storage_version(...)``.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, controller._persist_storage_version, configuration, version)
