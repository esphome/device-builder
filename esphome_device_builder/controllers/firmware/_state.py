"""Mutable domain state for :class:`FirmwareController`."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field

from ...models import FirmwareJob, JobSource, JobStatus, JobType


@dataclass
class Lane:
    """One serial work lane: its FIFO queue + the job/subprocess on it now.

    Two lanes run concurrently — a compile lane (CPU) and an upload lane
    (network) — so a slow network flash doesn't block the next compile.
    Within a lane work stays serialized (``current_job`` is the single slot).
    """

    queue: asyncio.Queue[FirmwareJob] = field(default_factory=asyncio.Queue)
    current_job: FirmwareJob | None = None
    current_process: asyncio.subprocess.Process | None = None


@dataclass
class RemoteDispatchState:
    """The remote build-server pool — owned by ``remote_dispatch``.

    A pooled compile moves through three states, each transition a method
    here so callers never juggle the dicts in lock-step: ``hold`` (waiting
    in ``pending``), ``start`` (in-flight on a server: ``in_flight`` task +
    ``job_peer`` pin), and ``release`` / ``drop`` (gone). ``busy_pins`` is
    the scheduler's exclusion set; ``record_loss`` / ``forget_losses`` cap
    mid-build re-routes. ``wake`` kicks the matcher on every change.
    """

    pending: dict[str, FirmwareJob] = field(default_factory=dict)
    in_flight: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    job_peer: dict[str, str] = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    retries: dict[str, int] = field(default_factory=dict)

    def busy_pins(self) -> frozenset[str]:
        """Pins driving an in-flight compile — the scheduler's busy set."""
        return frozenset(self.job_peer.values())

    def is_in_flight(self, job_id: str) -> bool:
        """Whether *job_id* is an in-flight remote compile (off-lane, driven by a task)."""
        return job_id in self.in_flight

    def hold(self, job: FirmwareJob) -> None:
        """Add *job* to the waiting set and wake the matcher."""
        self.pending[job.job_id] = job
        self.wake.set()

    def drop(self, job_id: str) -> None:
        """Remove a waiting job (cancel / supersede / re-routed to the lane)."""
        self.pending.pop(job_id, None)
        self.retries.pop(job_id, None)

    def discard(self, job_id: str) -> None:
        """Remove *job_id* from the pool entirely — waiting *or* in-flight — and wake.

        Used by cancel / supersede: ``drop`` handles the common waiting case,
        but a job can be bound in-flight before it's stamped RUNNING under a
        non-eager scheduler, so clear that binding too (the driver's terminal
        guard then skips the build).
        """
        self.drop(job_id)
        self.release(job_id)

    def start(self, job: FirmwareJob, pin_sha256: str, task: asyncio.Task[None]) -> None:
        """Move *job* from waiting to in-flight on server *pin_sha256*."""
        self.pending.pop(job.job_id, None)
        self.job_peer[job.job_id] = pin_sha256
        self.in_flight[job.job_id] = task

    def release(self, job_id: str) -> None:
        """Clear an in-flight job's server binding and wake the matcher (its server freed)."""
        self.in_flight.pop(job_id, None)
        self.job_peer.pop(job_id, None)
        self.wake.set()

    def rearm_if_pending(self) -> None:
        """Wake the matcher if a compile is waiting; a no-op (cheap) when nothing's pending."""
        if self.pending:
            self.wake.set()

    def record_loss(self, job_id: str) -> int:
        """Count a mid-build server loss for *job_id*; return the running total."""
        self.retries[job_id] = self.retries.get(job_id, 0) + 1
        return self.retries[job_id]

    def forget_losses(self, job_id: str) -> None:
        """Drop *job_id*'s loss counter once it reaches a real terminal."""
        self.retries.pop(job_id, None)


@dataclass
class FirmwareState:
    """Mutable state for :class:`FirmwareController`."""

    # ``esphome`` CLI invocation discovered at ``start()`` —
    # ``[sys.executable, "-m", "esphome"]`` or the on-PATH
    # ``esphome`` binary, whichever ``_find_esphome_cmd``
    # picks first. ``cli`` reads it to build the subprocess argv.
    esphome_cmd: list[str] = field(default_factory=list)

    # The two concurrent lanes. Producers enqueue onto the lane a job's
    # type maps to (``_lane_for``); each lane has its own consumer task.
    # Both survive restarts via the on-disk persistence layer.
    compile_lane: Lane = field(default_factory=Lane)
    upload_lane: Lane = field(default_factory=Lane)

    # Active + recent jobs keyed by ``job_id``. ``persistence``
    # reads / writes on every state transition; ``clean``,
    # ``follow``, ``jobs``, ``factories``, and ``lifecycle`` read
    # for lookup. Trimmed to history limits by
    # ``persistence._prune_history``.
    jobs: dict[str, FirmwareJob] = field(default_factory=dict)

    # Job ids the user asked to cancel; the runner consults this
    # on subprocess exit to mark CANCELLED instead of FAILED.
    cancel_requested: set[str] = field(default_factory=set)

    # Per-job wake event for the remote runner — set by the
    # cancel handler so a remote job waiting on its terminal
    # frame unblocks instantly. The local subprocess path uses
    # SIGTERM instead and doesn't register here.
    cancel_events: dict[str, asyncio.Event] = field(default_factory=dict)

    # Set on every job terminal to wake the upload lane's build-tree gate
    # (see ``upload_blocked`` / ``runner._await_build_gate``).
    build_gate: asyncio.Event = field(default_factory=asyncio.Event)

    # Remote build-server pool (see :class:`RemoteDispatchState`).
    remote_dispatch: RemoteDispatchState = field(default_factory=RemoteDispatchState)

    def lane_for(self, job: FirmwareJob) -> Lane:
        """Return the lane *job* runs on: UPLOAD on the network lane, else the compile lane."""
        return self.upload_lane if job.job_type is JobType.UPLOAD else self.compile_lane

    def place_on_lane(self, job: FirmwareJob) -> None:
        """Route *job* to its worker: the remote pool for a pending remote compile, else its lane.

        A ``REMOTE_PENDING`` compile holds in the pool for a free build
        server instead of occupying the single compile lane; everything
        else goes on its lane. The single router so a job is never left
        double-tracked — anything bound for a lane is dropped from the pool.
        """
        if job.depends_on:
            # Reached only once the dependency is satisfied; latch it.
            job.dependency_released = True
        if job.source is JobSource.REMOTE_PENDING and job.job_type is JobType.COMPILE:
            self.remote_dispatch.hold(job)
            return
        self.remote_dispatch.drop(job.job_id)
        self.lane_for(job).queue.put_nowait(job)

    def request_cancel(self, job_id: str) -> None:
        """Flag *job_id* for cancellation and wake any runner parked on its cancel event.

        Only the remote runner registers a ``cancel_events`` entry; the local
        subprocess path's wake is SIGTERM on the spawned process, so a missing
        event is normal.
        """
        self.cancel_requested.add(job_id)
        event = self.cancel_events.get(job_id)
        if event is not None:
            event.set()

    def active_jobs(self) -> Iterator[FirmwareJob]:
        """Yield the queued or running jobs (skips terminal history)."""
        return (j for j in self.jobs.values() if j.is_active)

    def dependency_satisfied(self, job: FirmwareJob) -> bool:
        """Return whether *job* has no prerequisite, or its prerequisite has completed."""
        if not job.depends_on:
            return True
        prereq = self.jobs.get(job.depends_on)
        return prereq is not None and prereq.status is JobStatus.COMPLETED

    def upload_blocked(self, job: FirmwareJob) -> bool:
        """Whether an UPLOAD must wait for an in-flight clean/reset to finish first.

        A clean/reset rmtree's build artifacts that ``esphome upload`` reads;
        since they run on separate lanes, an unguarded upload could flash a
        truncated binary mid-wipe. RESET_BUILD_ENV (whole tree) blocks every
        upload; a CLEAN blocks only its own configuration's upload.
        """
        if job.job_type is not JobType.UPLOAD:
            return False
        for other in self.active_jobs():
            if other.job_type is JobType.RESET_BUILD_ENV:
                return True
            if other.job_type is JobType.CLEAN and other.configuration == job.configuration:
                return True
        return False
