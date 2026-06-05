"""Firmware-job clean: local clean + fan-out to connected paired receivers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...models import (
    FirmwareJob,
    JobBuildSource,
    JobType,
)
from ...models.remote_build import PeerStatus

if TYPE_CHECKING:
    from .controller import FirmwareController


async def clean(controller: FirmwareController, *, configuration: str) -> FirmwareJob:
    """
    Queue a clean job + one per connected paired receiver; return the LOCAL job.

    The LOCAL clean's default supersede cancels any in-flight build for the
    same configuration (a clean is the user asking for a fresh build). Per-peer
    REMOTE clean jobs surface through the firmware-jobs ``subscribe_events``
    stream, not the WS reply.
    """
    await controller._validate_configuration_boundary(configuration)
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
            build_source=JobBuildSource.for_server(
                pin_sha256=pairing.pin_sha256,
                label=pairing.label,
                esphome_version=pairing.esphome_version,
            ),
        )
        # ``supersede=False``: the fan-out batch is N+1 jobs sharing
        # one ``configuration``; default supersede would leave only
        # the LAST peer's clean alive.
        await controller._enqueue(remote_job, supersede=False)
