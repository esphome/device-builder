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
# A ``firmware/clean`` request that lands while one of these is
# in-flight for the same configuration is rejected loudly rather
# than supersede-cancelled — see the ``clean`` handler's docstring
# for the rationale.
_BUILD_PRODUCING_JOB_TYPES: frozenset[JobType] = frozenset(
    {JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL, JobType.RENAME}
)


async def clean(controller: FirmwareController, *, configuration: str) -> FirmwareJob:
    """
    Queue a build clean job, plus one per connected paired receiver.

    Returns the LOCAL clean job (the one the operator's WS
    command is awaiting). N additional REMOTE clean jobs are
    queued silently for fan-out to every currently-connected
    approved peer; each shows up as its own
    :class:`FirmwareJob` in the firmware-jobs list and drives
    the same lifecycle events as remote installs do, so the
    operator sees per-receiver clean progress in the
    existing UI.

    **Why fan out:** a stale receiver-side build dir is the
    same class of problem a stale local build dir is. The
    operator's "Clean build files" click expects every place
    this device has been built to drop its artifacts, not
    just the local one. Without the fan-out, a remote receiver
    keeps caching the broken state and the next remote
    compile picks up the same poisoned tree.

    **Best-effort:** a peer that disconnects between this
    ``clean`` call and the runner picking up its job lands on
    the existing remote-session-lost FAILED path (the runner's
    ``_dispatch_and_drive`` returns ``CommandError`` from
    ``_lookup_open_peer_link_client``). The local job is
    independent and runs regardless. A peer that isn't
    connected at all just doesn't get a job queued — the next
    time the operator clicks clean while that peer is
    connected, it'll catch up.

    Rejects with ``CommandError(INVALID_ARGS)`` when an active
    compile / upload / install / rename job exists for the same
    configuration. Other firmware commands rely on the
    ``_enqueue`` supersede path to cancel-and-replace the running
    job — that's the right shape for "user wants to retry the
    compile" — but a clean wipes the build artifacts the running
    job is producing, so a quietly-cancelled build that the user
    didn't intend to abandon is the worse failure mode. Make the
    user retry once the build settles instead. Two clean jobs
    for the same configuration still supersede each other (the
    second one is the user's intent regardless). The supersede
    check applies only to the LOCAL job; the fan-out's per-peer
    REMOTE jobs enqueue with ``supersede=False`` so they don't
    cancel siblings or the just-queued local clean. See
    :meth:`_enqueue`'s docstring for the carve-out rationale.

    The WS reply returns only the LOCAL clean — that's what the
    operator's ``firmware/clean`` call awaits. Per-peer REMOTE
    clean jobs surface through the existing
    ``subscribe_events`` firmware-jobs stream the dashboard
    already consumes for in-flight job lists, so the operator
    sees N+1 rows in the firmware-tasks panel without the
    handler needing to thread them through the WS reply
    shape. Don't "fix" this to return a list — the WS contract
    is "the handler returns the job the operator's click
    produced"; the fan-out is incidental.

    Multi-offloader fleets: a clean from offloader A and a
    concurrent compile from offloader B against the same
    receiver are safe by construction. Each offloader gets its
    own ``ESPHOME_DATA_DIR`` subtree
    (``<receiver_data_dir>/.remote_builds/<dashboard_id>/.esphome``),
    so A's clean only wipes A's per-offloader build dir; B's
    compile artefacts under B's subtree are untouched. The
    receiver-side single-flight queue serializes the actual
    subprocess invocations regardless, but the per-offloader
    isolation is what makes the cross-offloader race a
    non-issue at the filesystem level.
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
    """Queue one REMOTE clean job per connected approved peer.

    Reads the remote-build controller's RAM-canonical
    ``(_pairings, _open_peer_links)`` state via
    :meth:`OffloaderController.build_scheduler_snapshot`.
    Approved + connected peers get a job each; everything else
    is silently skipped (a PENDING row can't accept submits,
    a disconnected approved row would just FAIL on the runner's
    first ``_lookup_open_peer_link_client``).

    Fan-out is silent on the WS reply — the operator's
    ``firmware/clean`` call returns the local job; the remote
    jobs surface through the existing
    firmware-jobs subscribe-events stream the dashboard already
    consumes for in-flight job lists. A regression that lost
    the fan-out shows up as "I clicked Clean but my receiver
    still has the old build".
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
        # ``supersede=False``: the fan-out batch is N+1 jobs
        # all sharing one ``configuration``, so default
        # supersede semantics ("cancel any prior active job
        # for this configuration") would cancel the local
        # clean we just queued plus every prior fan-out
        # sibling, leaving only the LAST peer's clean alive.
        # See ``_enqueue``'s docstring for the carve-out
        # rationale.
        await controller._enqueue(remote_job, supersede=False)


def _active_build_for(controller: FirmwareController, configuration: str) -> FirmwareJob | None:
    """Return any in-flight build-producing job on *configuration*.

    Filters ``_jobs`` by status (``_ACTIVE_JOB_STATUSES``) and
    type (``_BUILD_PRODUCING_JOB_TYPES``). Used by ``clean`` to
    reject rather than supersede when a destructive op would
    wipe artifacts the running job is producing.
    """
    for active in controller._jobs.values():
        if active.configuration != configuration:
            continue
        if active.status not in _ACTIVE_JOB_STATUSES:
            continue
        if active.job_type in _BUILD_PRODUCING_JOB_TYPES:
            return active
    return None
