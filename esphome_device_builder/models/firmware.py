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


class JobSource(StrEnum):
    """
    Where a :class:`FirmwareJob`'s bytes come from.

    ``LOCAL`` is a build this dashboard's CPU ran. ``REMOTE``
    is a build a paired receiver ran and this dashboard
    fetched the artifacts from. Distinct from
    :class:`JobType` ("what operation: compile / upload /
    install"); ``source`` answers "who did the compile."
    """

    LOCAL = "local"
    REMOTE = "remote"


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
    # (0-100). Monotonically non-decreasing *within a phase* — the
    # streaming ingest only latches a higher parsed percent. At
    # known phase seams (REMOTE install's compile → upload boundary
    # in :func:`controllers.firmware.remote_runner._fetch_and_run_local_upload`)
    # the runner explicitly resets to 0 so subsequent phase percents
    # aren't silently clamped against the previous phase's peak.
    # ``None`` when the underlying tooling hasn't emitted a percentage
    # yet -- most compile output is opaque, but the heavy phases (PIO
    # build, esptool flash) do emit percentages we can latch onto.
    progress: int | None = None
    # Offloader's ``dashboard_id`` when this job came in via the
    # peer-link ``submit_job`` flow (issue #106). Empty for
    # locally-submitted jobs. Surfaced in the firmware-tasks UI
    # as a "from <peer>" badge so the receiver-side admin can
    # tell their own work apart from delegated builds.
    remote_peer: str = ""
    # Offloader's job_id from the ``submit_job`` header. Empty for
    # locally-submitted jobs. The receiver-side ``job_id`` above
    # is generated independently (uuid4 hex) so the two id-spaces
    # don't collide; this field carries the offloader's tag so
    # the receiver-side fan-out path can echo it back on
    # ``job_state_changed`` / ``job_output`` frames — the
    # offloader matches against its own submit-tagged id, not
    # the receiver's local one.
    remote_job_id: str = ""
    # Display label for the offloader that submitted this job,
    # when ``remote_peer`` is set. Empty for locally-submitted
    # jobs and for offloader-side rows.
    # Snapshot of :attr:`StoredPeer.label` at submit time —
    # doesn't track later renames of the peer's label (the
    # log entry reflects what was true when the work landed).
    # Symmetric to :attr:`source_label` on the offloader side:
    # both surfaces want a human handle on the OTHER half of
    # the pair without re-querying that half's mutable state.
    remote_peer_label: str = ""
    # The device's ``esphome.name`` (machine handle) and
    # ``esphome.friendly_name`` (display string). Carried on
    # the receiver-side row only — the offloader puts both on
    # the wire via the :class:`SubmitJobFrameData` header
    # (``device_name`` / ``device_friendly_name``) because it
    # already has them off its local Device scanner at install
    # time; the receiver doesn't re-parse the bundled YAML.
    # Peer-controlled input on the receiver side — coerced +
    # length-capped by
    # :func:`controllers.remote_build.submit_job._coerce_display_field`
    # before landing here so a malicious / buggy header can't
    # ship a non-string or a multi-megabyte value through to
    # the firmware-tasks WS stream.
    #
    # The configuration field carries the full
    # ``.esphome/.remote_builds/<id>/<device>/...`` path which
    # is useless as a title; these fields let the firmware-
    # tasks UI render the device's actual name and friendly
    # name instead. Empty for locally-submitted jobs (the
    # dashboard's own Device list already knows the friendly
    # name for those — no need to duplicate it on the job),
    # and empty for receiver-side jobs whose offloader didn't
    # set the ``NotRequired`` wire fields (older offloader)
    # or whose YAML legitimately doesn't define
    # ``esphome.friendly_name``. The frontend's title surface
    # falls back from ``device_friendly_name`` → ``device_name``
    # → configuration-path device segment.
    device_name: str = ""
    device_friendly_name: str = ""
    # Where the build's bytes come from. The offloader-side
    # firmware-queue runner branches on this to choose its
    # pipeline (local subprocess vs peer-link dispatch).
    # Defaults to LOCAL so on-disk jobs from before this field
    # existed deserialise correctly. Distinct from
    # ``remote_peer`` / ``remote_job_id`` — those are
    # receiver-side, set when a receiver picks up an
    # offloader's ``submit_job``; ``source`` / ``source_pin_sha256``
    # / ``source_label`` are the offloader-side fields for the
    # same delegation seen from the dispatching dashboard.
    source: JobSource = JobSource.LOCAL
    # Machine-readable handle on the receiver that compiled
    # this job, when ``source == REMOTE``. Matches
    # :attr:`StoredPairing.pin_sha256` — the stable
    # cryptographic identity, NOT the user-mutable display
    # label. Load-bearing for restart recovery: the runner
    # picks up an in-progress REMOTE job after a dashboard
    # restart and needs to know which receiver to query /
    # cancel / download from, and
    # ``OffloaderController._open_peer_links`` is RAM-only
    # so the mapping can't be reconstructed otherwise.
    source_pin_sha256: str = ""
    # Display label for the paired receiver that compiled this
    # job, when ``source == REMOTE``. Empty for ``LOCAL`` jobs.
    # Snapshot of :attr:`StoredPairing.label` at job-creation
    # time — doesn't track later renames of the pairing label
    # (the timeline the user saw when they clicked Install is
    # what they expect to see in the log). Lookups go through
    # ``source_pin_sha256``; ``source_label`` is purely for
    # rendering.
    source_label: str = ""

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
          / ``job_id`` / ``source`` / ``source_pin_sha256`` /
          ``source_label`` / ``remote_peer`` / ``remote_peer_label`` /
          ``remote_job_id`` / ``device_name`` /
          ``device_friendly_name`` describe the job rather than
          the run, so they stay
          intact.
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
    esptool output. The streaming ingest only fires this event
    when the parsed percent advances, so the gauge climbs
    monotonically *within a phase*. At known phase seams
    (REMOTE install's compile → upload boundary —
    :func:`controllers.firmware.remote_runner._fetch_and_run_local_upload`)
    the runner explicitly fires a ``progress=0`` reset so the
    next phase's percents don't get clamped against the
    previous phase's peak. Subscribers should render the bar
    from the latest event rather than asserting non-decreasing
    progress.
    """

    job_id: str
    progress: int
