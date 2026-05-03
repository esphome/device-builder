"""Firmware job models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mashumaro.mixins.orjson import DataClassORJSONMixin


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
