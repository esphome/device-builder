"""Firmware-job clean: local clean + fan-out to connected paired receivers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...helpers.api import CommandError
from ...models import (
    ErrorCode,
    FirmwareJob,
    JobBuildSource,
    JobSource,
    JobType,
)
from ...models.remote_build import PeerStatus
from .constants import _ACTIVE_JOB_STATUSES

if TYPE_CHECKING:
    from .controller import FirmwareController


# Job types that produce build artifacts a clean would destroy.
# A clean is rejected loudly (not supersede-cancelled) while any
# of these is in-flight for the same configuration.
_BUILD_PRODUCING_JOB_TYPES: frozenset[JobType] = frozenset(
    {JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL, JobType.RENAME}
)


async def clean(controller: FirmwareController, *, configuration: str) -> FirmwareJob:
    """
    Queue a clean job + one per connected paired receiver; return the LOCAL job.

    Per-peer REMOTE clean jobs surface through the firmware-jobs
    ``subscribe_events`` stream, not the WS reply. Rejects with
    ``CommandError(INVALID_ARGS)`` while a compile / upload /
    install / rename is in flight for the same configuration —
    supersede would silently abandon a build the user didn't
    mean to cancel. Two clean jobs still supersede each other.
    """
    await controller._validate_configuration_boundary(configuration)
    if blocker := _active_build_for(controller, configuration):
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            f"{blocker.job_type.value} job already in progress "
            f"for {configuration}; wait for it to finish or "
            f"cancel it before cleaning.",
        )
    local_job = controller._create_job(configuration, JobType.CLEAN)
    enqueued = await controller._enqueue(local_job)
    await _fan_out_clean_to_connected_peers(controller, configuration)
    return enqueued


async def _fan_out_clean_to_connected_peers(
    controller: FirmwareController, configuration: str
) -> None:
    """
    Queue one REMOTE clean job per APPROVED+connected paired peer.

    Silently skips PENDING pairings and disconnected approved peers.
    """
    offloader = controller._db.remote_build_offloader
    if offloader is None:
        return
    snapshot = offloader.build_scheduler_snapshot()
    # ``build_scheduler_snapshot`` ``dict(self._pairings)``-copies
    # on construction, so iteration is already isolated from a
    # concurrent unpair landing on a different loop tick.
    for pairing in snapshot.pairings.values():
        if pairing.status is not PeerStatus.APPROVED:
            continue
        if pairing.pin_sha256 not in snapshot.open_peer_links:
            continue
        remote_job = controller._create_job(
            configuration,
            JobType.CLEAN,
            build_source=JobBuildSource(
                source=JobSource.REMOTE,
                source_pin_sha256=pairing.pin_sha256,
                source_label=pairing.label,
                source_esphome_version=pairing.esphome_version,
            ),
        )
        # ``supersede=False``: the fan-out batch is N+1 jobs sharing
        # one ``configuration``; default supersede would leave only
        # the LAST peer's clean alive.
        await controller._enqueue(remote_job, supersede=False)


def _active_build_for(controller: FirmwareController, configuration: str) -> FirmwareJob | None:
    """Return any in-flight build-producing job on *configuration*, else None."""
    for active in controller.state.jobs.values():
        if active.configuration != configuration:
            continue
        if active.status not in _ACTIVE_JOB_STATUSES:
            continue
        if active.job_type in _BUILD_PRODUCING_JOB_TYPES:
            return active
    return None
