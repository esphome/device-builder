"""Mutable domain state for :class:`FirmwareController`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ...models import FirmwareJob, JobStatus, JobType


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

    def lane_for(self, job: FirmwareJob) -> Lane:
        """Return the lane *job* runs on: UPLOAD on the network lane, else the compile lane."""
        return self.upload_lane if job.job_type is JobType.UPLOAD else self.compile_lane

    def dependency_satisfied(self, job: FirmwareJob) -> bool:
        """Return whether *job* has no prerequisite, or its prerequisite has completed."""
        if not job.depends_on:
            return True
        prereq = self.jobs.get(job.depends_on)
        return prereq is not None and prereq.status is JobStatus.COMPLETED

    # Transitional back-compat shims: the legacy single-lane attribute
    # names proxy to the compile lane (historically the everything/CPU
    # lane). Lets call sites and tests that predate lanes keep reading
    # ``state.queue`` / ``state.current_job`` / ``state.current_process``.
    @property
    def queue(self) -> asyncio.Queue[FirmwareJob]:
        return self.compile_lane.queue

    @queue.setter
    def queue(self, value: asyncio.Queue[FirmwareJob]) -> None:
        self.compile_lane.queue = value

    @property
    def current_job(self) -> FirmwareJob | None:
        return self.compile_lane.current_job

    @current_job.setter
    def current_job(self, value: FirmwareJob | None) -> None:
        self.compile_lane.current_job = value

    @property
    def current_process(self) -> asyncio.subprocess.Process | None:
        return self.compile_lane.current_process

    @current_process.setter
    def current_process(self, value: asyncio.subprocess.Process | None) -> None:
        self.compile_lane.current_process = value
