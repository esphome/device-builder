"""Mutable domain state for :class:`FirmwareController`."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field

from ...models import FirmwareJob, JobStatus, JobType
from .constants import _ACTIVE_JOB_STATUSES


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

    def lane_for(self, job: FirmwareJob) -> Lane:
        """Return the lane *job* runs on: UPLOAD on the network lane, else the compile lane."""
        return self.upload_lane if job.job_type is JobType.UPLOAD else self.compile_lane

    def place_on_lane(self, job: FirmwareJob) -> None:
        """Put *job* onto the lane its type maps to, ready for that lane's consumer."""
        if job.depends_on:
            # Reached only once the dependency is satisfied; latch it.
            job.dependency_released = True
        self.lane_for(job).queue.put_nowait(job)

    def active_jobs(self) -> Iterator[FirmwareJob]:
        """Yield the queued or running jobs (skips terminal history)."""
        return (j for j in self.jobs.values() if j.status in _ACTIVE_JOB_STATUSES)

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
