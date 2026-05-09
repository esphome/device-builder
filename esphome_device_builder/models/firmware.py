"""Firmware job models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypedDict

from mashumaro.mixins.orjson import DataClassORJSONMixin

from .common import EventType


class JobStatus(StrEnum):
    """Firmware job status."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    """Firmware job type."""

    COMPILE = "compile"
    UPLOAD = "upload"
    INSTALL = "install"  # compile + upload in one step
    CLEAN = "clean"
    # Wipes ``.esphome/build/``, ``external_components/``, and
    # ``platformio_cache/`` — forces the next compile to re-download
    # toolchains and re-fetch external components from scratch.
    RESET_BUILD_ENV = "reset_build_env"
    # ``esphome rename`` — internally validates, writes a new YAML,
    # compiles, OTA-installs the new firmware, and only then drops
    # the old YAML. Routed through the firmware queue so it shows up
    # in the firmware-tasks list with live output instead of running
    # silently in the background.
    RENAME = "rename"


# Terminal job states — a job in any of these isn't running and
# isn't waiting to run.
TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)

# Lifecycle events that match ``TERMINAL_JOB_STATUSES``. The runner
# fires exactly one of these per job, matching the status set
# above — kept as a separate constant because subscriptions key
# off ``EventType`` while state checks key off ``JobStatus``.
TERMINAL_JOB_EVENTS: frozenset[EventType] = frozenset(
    {EventType.JOB_COMPLETED, EventType.JOB_FAILED, EventType.JOB_CANCELLED}
)


@dataclass
class FirmwareJob(DataClassORJSONMixin):
    """A firmware build/upload job.

    Jobs are persistent (survive page refreshes and server restarts)
    and decoupled from WebSocket connections. Output is buffered so
    clients can reconnect and catch up.
    """

    job_id: str
    configuration: str  # device yaml filename
    job_type: JobType
    status: JobStatus = JobStatus.QUEUED
    created_at: str = ""  # ISO 8601
    started_at: str | None = None
    completed_at: str | None = None
    exit_code: int | None = None
    output: list[str] = field(default_factory=list)
    error: str | None = None
    port: str = ""  # for upload jobs
    # New device name for ``rename`` jobs. Plumbed through to the
    # ``esphome rename`` CLI. Empty for every other job type.
    new_name: str = ""
    # Coarse progress estimate parsed from PlatformIO/esptool output
    # (0-100, monotonically non-decreasing while the job runs).
    # ``None`` when the underlying tooling hasn't emitted a percentage
    # yet -- most compile output is opaque, but the heavy phases (PIO
    # build, esptool flash) do emit percentages we can latch onto.
    progress: int | None = None

    def reset(self) -> None:
        """
        Reset per-run state so the job is ready to be re-executed.

        Called by the persistence-load path when a ``RUNNING`` job
        survives a dashboard restart and is being re-queued for a
        fresh run. Lives on the model (not as a free helper) so
        every place that adds a per-run-state field is forced to
        consider whether it should clear here too — without that,
        a future field that defaults to ``None`` and gets set by
        the runner would silently leak the crashed run's value
        into the rebuild's status display.

        Behaviour:

        - **Keeps ``output``** — the pre-crash log is useful
          diagnostic history. Appends a marker line so a
          follower tailing the merged buffer can see exactly
          where the rebuild starts.
        - **Clears per-run state** — ``progress`` / ``error`` /
          ``started_at`` / ``completed_at`` / ``exit_code``
          back to their defaults.
        - **Doesn't change ``status``** — the caller decides
          the transition (load path flips ``RUNNING`` →
          ``QUEUED``; future callers might want a different
          target).
        - **Preserves identity** — ``configuration`` /
          ``job_type`` / ``port`` / ``new_name`` / ``created_at``
          / ``job_id`` describe the job rather than the run, so
          they stay intact.
        """
        self.output = [*self.output, _RECOVERY_NOTICE]
        self.progress = None
        self.error = None
        self.started_at = None
        self.completed_at = None
        self.exit_code = None


_RECOVERY_NOTICE = (
    "... [dashboard restarted mid-build; the previous run's log is above, "
    "the rebuild begins below] ...\n"
)


# ---------------------------------------------------------------------------
# Event payload shapes (TypedDict so the bus.fire data dict is
# type-checked at the call site without changing the wire shape;
# mirrors HA's ``EventStateChangedData`` / ``EventStateReportedData``
# pattern). See ``docs/ARCHITECTURE.md`` "Event bus → Typing event
# payloads" for the subscriber-side narrowing pattern.
# ---------------------------------------------------------------------------


class JobLifecycleData(TypedDict):
    """
    Payload for the five terminal-or-lifecycle ``EventType.JOB_*`` events.

    ``EventType.JOB_QUEUED`` / ``JOB_STARTED`` / ``JOB_COMPLETED`` /
    ``JOB_FAILED`` / ``JOB_CANCELLED`` share a single shape;
    subscribers differentiate by the ``EventType`` carried
    alongside, not by inspecting the payload. The full
    ``FirmwareJob`` rides through so the frontend's job-table
    renderer has every field it needs (status, exit_code,
    progress, output) without an additional fetch.
    """

    job: FirmwareJob


class JobOutputData(TypedDict):
    r"""
    Payload for ``EventType.JOB_OUTPUT``.

    One event per output chunk of a running subprocess. ``job_id``
    keys the chunk to its job; ``line`` is the raw stdout/stderr
    text *with its trailing terminator preserved* — ``\n``,
    ``\r``, or ``\r\n`` (see ``iter_lines_with_progress`` for why
    the terminator rides through). Carriage-return-only chunks
    are esptool / PlatformIO progress overwrites; the frontend's
    ansi-log renderer leans on the distinction to decide whether
    to append a new line or overwrite the last one. The
    ``follow_job`` / ``stream_logs`` streams push these through
    verbatim.
    """

    job_id: str
    line: str


class JobProgressData(TypedDict):
    """
    Payload for ``EventType.JOB_PROGRESS``.

    Coarse 0-100 progress estimate parsed from PlatformIO /
    esptool output. ``progress`` is monotonically non-decreasing
    while the job runs (the runner only fires when the parsed
    percentage advances). The dashboard renders this as a
    progress bar in the firmware-tasks panel.
    """

    job_id: str
    progress: int
