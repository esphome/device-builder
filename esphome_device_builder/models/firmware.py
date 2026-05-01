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
    # Coarse progress estimate parsed from PlatformIO/esptool output
    # (0-100, monotonically non-decreasing while the job runs).
    # ``None`` when the underlying tooling hasn't emitted a percentage
    # yet -- most compile output is opaque, but the heavy phases (PIO
    # build, esptool flash) do emit percentages we can latch onto.
    progress: int | None = None
