"""
Remote build-server pool: bind ``REMOTE_PENDING`` compiles to a free server.

A ``REMOTE_PENDING`` compile (``factories.resolve_install_source``)
holds in ``state.remote_dispatch.pending`` instead of occupying the
single compile lane. One long-lived loop (``run_dispatch_loop``,
gathered in ``FirmwareController._run_queue``) wakes on enqueue, a
peer connecting, or a server freeing, re-snapshots the offloader's
live pairing/queue state, and matches each waiting compile to a free
server — so paired servers compile concurrently and a host paired or
freed mid-queue is used without a fresh ``firmware/install``.

The *which-server* choice is made here at dispatch (not at submit),
which is the whole point: ``pick_dispatch_target`` re-runs against the
current pool every pass, with the servers already driving a job
excluded via ``busy_build_server_pins``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from ...helpers.build_scheduler import DispatchOutcome, pick_dispatch_target
from ...models import (
    LOCAL_JOB_BUILD_SOURCE,
    TERMINAL_JOB_STATUSES,
    EventType,
    FirmwareJob,
    JobBuildSource,
    JobStatus,
)
from . import lifecycle
from .helpers import _ingest_output_line
from .remote_runner import RemoteServerLostError, run_remote_job

if TYPE_CHECKING:
    from ...helpers.build_scheduler import BuildSchedulerInputs
    from ...helpers.event_bus import Event
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)

# A compile whose server vanishes mid-build re-routes to another worker;
# bound the re-routes so a single flapping server can't loop it forever.
_MAX_SERVER_LOSS_RETRIES = 3

# Hold the first dispatch pass this long after startup so paired build servers
# have time to reconnect (offloader.start() runs after firmware.start(), and the
# peer-link handshakes take a moment). Without it, restored REMOTE_PENDING
# compiles would fall back to LOCAL because no server is connected yet. Patched
# to 0 in tests. Module constant so it can be tuned without touching the loop.
_STARTUP_GRACE_SECONDS = 20.0

# Offloader bus events that change which servers are available: a peer
# connecting/leaving, a receiver flipping idle/busy, and pairing-lifecycle
# changes (add / approve / unpair / enable / disable). Each just re-arms the
# matcher; the dispatch pass reads the fresh snapshot. The pairing events matter
# because a job WAITing on a disconnected intended server must re-evaluate if
# that server is then unpaired or disabled (no peer-link event fires then).
_POOL_WAKE_EVENTS = (
    EventType.OFFLOADER_PEER_LINK_OPENED,
    EventType.OFFLOADER_PEER_LINK_CLOSED,
    EventType.OFFLOADER_QUEUE_STATUS_CHANGED,
    EventType.OFFLOADER_PAIRING_ADDED,
    EventType.OFFLOADER_PAIR_STATUS_CHANGED,
    EventType.OFFLOADER_PAIRING_ENABLED_CHANGED,
)


async def run_dispatch_loop(controller: FirmwareController) -> None:
    """Match waiting remote compiles to free build servers until cancelled.

    Subscribes to the pool-wake events for the loop's lifetime so a
    newly-connected or freed server pulls the next pending compile;
    the subscription detaches when the loop task is cancelled at
    controller shutdown. Holds a startup grace before the first pass so
    servers reconnecting after a restart are used instead of falling back
    to local.
    """
    state = controller.state

    def _wake(_event: Event[object]) -> None:
        state.remote_dispatch.wake.set()

    with controller.bus.listening(_POOL_WAKE_EVENTS, _wake):
        # Listeners are attached first so a server connecting during the grace
        # still arms the wake for the post-grace pass.
        await asyncio.sleep(_STARTUP_GRACE_SECONDS)
        while True:
            await state.remote_dispatch.wake.wait()
            state.remote_dispatch.wake.clear()
            await _dispatch_pending(controller)


async def _dispatch_pending(controller: FirmwareController) -> None:
    """One matcher pass over the waiting compiles; persist if any landed terminal / local."""
    offloader = controller._db.remote_build_offloader
    if offloader is None:
        await _flush_to_local(controller)
        return
    snapshot = offloader.build_scheduler_snapshot()
    needs_persist = False
    for job in list(controller.state.remote_dispatch.pending.values()):
        needs_persist |= _dispatch_one(controller, job, snapshot)
    if needs_persist:
        await controller._persist_jobs()


def _dispatch_one(
    controller: FirmwareController, job: FirmwareJob, snapshot: BuildSchedulerInputs
) -> bool:
    """Act on one waiting compile; return whether it needs a persist (went local / failed)."""
    pool = controller.state.remote_dispatch
    if job.status is not JobStatus.QUEUED:
        # Cancelled / superseded between waking and here; its own handler
        # already finalised it, just drop the stale pool entry.
        pool.drop(job.job_id)
        return False
    # Re-derive busy each call so two waiting compiles in one pass can't
    # both grab the same just-freed server.
    inputs = replace(snapshot, busy_build_server_pins=pool.busy_pins())
    decision = pick_dispatch_target(inputs)
    if decision.outcome is DispatchOutcome.REMOTE:
        assert decision.pin_sha256 is not None  # narrowed by REMOTE
        _dispatch_to_server(controller, job, decision.pin_sha256)
        return False
    if decision.outcome is DispatchOutcome.LOCAL:
        _fallback_to_local(controller, job)
        return True
    if decision.outcome is DispatchOutcome.NO_COMPATIBLE_PEER:
        _fail_no_compatible_peer(controller, job, decision.message)
        return True
    return False  # WAIT — every server busy; hold for the next pass


async def _flush_to_local(controller: FirmwareController) -> None:
    """Remote build went away entirely — run every waiting compile locally, never strand one."""
    flushed = list(controller.state.remote_dispatch.pending.values())
    for job in flushed:
        _fallback_to_local(controller, job)
    if flushed:
        await controller._persist_jobs()


def _dispatch_to_server(controller: FirmwareController, job: FirmwareJob, pin_sha256: str) -> None:
    """Bind *job* to the server behind *pin_sha256* and spawn its driver."""
    offloader = controller._db.remote_build_offloader
    pairing = offloader.get_pairing(pin_sha256) if offloader is not None else None
    if pairing is None:
        # Raced an unpair between snapshot and bind; leave it pending and
        # re-arm the matcher ourselves so a missed peer-link-close event
        # can't strand the compile (the next pass picks a live server).
        controller.state.remote_dispatch.wake.set()
        return
    job.apply_build_source(
        JobBuildSource.for_server(
            pin_sha256=pairing.pin_sha256,
            label=pairing.label,
            esphome_version=pairing.esphome_version,
        )
    )
    # ``create_background_task`` is eager: ``_drive_remote`` runs its
    # ``begin_run`` prologue (stamps RUNNING, fires JOB_STARTED) synchronously
    # before ``start()`` records the in-flight entry. That's safe — no
    # JOB_STARTED listener reads pool state, and a cancel can't interleave the
    # sync prologue (it lands after ``start()``, where ``is_in_flight`` is true,
    # and ``run_remote_job`` replays ``cancel_requested`` once its event exists).
    task = controller._db.create_background_task(_drive_remote(controller, job))
    controller.state.remote_dispatch.start(job, pin_sha256, task)


def _fallback_to_local(controller: FirmwareController, job: FirmwareJob) -> None:
    """Flip *job* to LOCAL and route it onto the compile lane (no server reachable)."""
    job.apply_build_source(LOCAL_JOB_BUILD_SOURCE)
    controller.state.place_on_lane(job)


def _fail_no_compatible_peer(
    controller: FirmwareController, job: FirmwareJob, message: str
) -> None:
    """Finalise *job* FAILED — EXACT_REQUIRED with no compatible server left."""
    controller.state.remote_dispatch.drop(job.job_id)
    controller._finalize_terminal(job, JobStatus.FAILED, error=message)
    _LOGGER.warning("Remote compile %s failed: %s", job.job_id, message)


async def _drive_remote(controller: FirmwareController, job: FirmwareJob) -> None:
    """Run a bound remote compile off-lane, finalise, and free the server.

    Shares the lane runner's run ceremony via ``lifecycle.begin_run`` /
    ``end_run`` (there's no lane slot here); ``run_remote_job`` owns the
    terminal finalise and cancel.
    """
    pool = controller.state.remote_dispatch
    if job.status in TERMINAL_JOB_STATUSES:
        # Cancelled / superseded in the window between dispatch and this task
        # running — don't start the build, just free the slot. (The eager
        # scheduler stamps RUNNING before the in-flight binding so this can't
        # fire today, but it keeps the driver correct under any task timing.)
        pool.release(job.job_id)
        return
    await lifecycle.begin_run(controller, job)
    try:
        await run_remote_job(controller, job, retry_on_server_loss=True)
    except RemoteServerLostError as lost:
        _requeue_after_server_loss(controller, job, str(lost))
    except asyncio.CancelledError:
        # run_remote_job already finalised CANCELLED on its way out; just unwind.
        raise
    except Exception as exc:  # noqa: BLE001 — terminality guarantee; helper logs + finalizes
        # Mirror the lane runner: an unexpected raise must still finalize the
        # job, never leave it stuck RUNNING with no JOB_FAILED.
        lifecycle.finalize_unexpected_error(controller, job, exc)
    finally:
        pool.release(job.job_id)
        if job.status in TERMINAL_JOB_STATUSES:
            pool.forget_losses(job.job_id)
        await lifecycle.end_run(controller, job)


def _requeue_after_server_loss(
    controller: FirmwareController, job: FirmwareJob, reason: str
) -> None:
    """Re-route a compile whose server vanished mid-build, or fail it past the retry cap."""
    pool = controller.state.remote_dispatch
    attempt = pool.record_loss(job.job_id)
    if attempt > _MAX_SERVER_LOSS_RETRIES:
        error = f"remote build: server lost mid-build {attempt}x ({reason})"
        controller._finalize_terminal(job, JobStatus.FAILED, error=error)
        pool.forget_losses(job.job_id)
        return
    _ingest_output_line(
        job,
        controller.bus,
        f"\n*** build server lost ({reason}); re-routing to another worker ***\n",
    )
    _return_to_pool(controller, job)
    _LOGGER.info("Remote compile %s: server lost, re-routing (attempt %d)", job.job_id, attempt)


def _return_to_pool(controller: FirmwareController, job: FirmwareJob) -> None:
    """Reset a remote compile to ``REMOTE_PENDING`` and route it back to the pool."""
    job.revert_to_pending_remote()
    controller.state.place_on_lane(job)
