"""Firmware job models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import NamedTuple, TypedDict

from .common import DashboardModel, EventType


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string (the job-timestamp format)."""
    return datetime.now(UTC).isoformat()


class QueueStatus(NamedTuple):
    """Snapshot of one firmware lane's RAM state.

    A tuple subclass so the existing
    ``idle, running, queue_depth = ...`` unpacking on the
    receiver-side broadcast paths keeps working, plus named
    access (``snapshot.idle``) for test stubs.

    :meth:`FirmwareController.lane_status` returns one lane's
    snapshot; :meth:`FirmwareController.compile_queue_status` is
    the compile lane's, which the remote-build scheduler keys on.
    """

    idle: bool
    running: bool
    queue_depth: int


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
    # Retained so older persisted INSTALL jobs still deserialise and run as a
    # fused ``esphome run`` (runner + CLI). New installs enqueue a COMPILE + a
    # dependent UPLOAD instead.
    INSTALL = "install"
    CLEAN = "clean"
    # Wipes ``.esphome/build/``, ``external_components/``, and
    # ``platformio_cache/`` — forces the next compile to re-download
    # toolchains and re-fetch external components from scratch.
    RESET_BUILD_ENV = "reset_build_env"
    # The flash-and-swap tail of a rename chain (``depends_on`` a
    # COMPILE of the renamed YAML): OTA-uploads the new firmware to
    # the old device address, then drops the old YAML. A persisted
    # RENAME with no ``depends_on`` predates the decomposition and
    # still runs the fused ``esphome rename`` CLI.
    RENAME = "rename"


# Job types that compile firmware from source (INSTALL is a fused compile +
# flash; UPLOAD only flashes an existing binary), so the config hash changes
# and a version-mismatched remote build must provision the offloader's esphome.
COMPILING_JOB_TYPES: frozenset[JobType] = frozenset({JobType.COMPILE, JobType.INSTALL})


class JobSource(StrEnum):
    """
    Where a :class:`FirmwareJob`'s bytes come from.

    ``LOCAL`` is a build this dashboard's CPU ran. ``REMOTE``
    is a build a paired receiver ran and this dashboard
    fetched the artifacts from. Distinct from
    :class:`JobType` ("what operation: compile / upload /
    install"); ``source`` answers "who did the compile."

    ``REMOTE_PENDING`` is the offloader-only transient between
    enqueue and dispatch: a compile that *will* go to a paired
    server, but whose server is chosen at dispatch (so a host
    paired/freed mid-queue is picked up). The remote-dispatch
    pool resolves it to ``REMOTE`` (with a pin) or, if no server
    is reachable, to ``LOCAL`` before the build runs; it never
    crosses the wire to a receiver.
    """

    LOCAL = "local"
    REMOTE = "remote"
    REMOTE_PENDING = "remote_pending"


class JobFailureReason(StrEnum):
    """Machine-readable failure category set alongside a FAILED terminal.

    Distinct from :attr:`FirmwareJob.error`, the human-readable message.
    ``NONE`` is an ordinary failure (compile error, bad YAML, …) the offloader
    surfaces as-is; ``PROVISION`` means a receiver couldn't provision the target
    esphome, which the offloader treats as retryable and rebuilds locally. New
    retryable-vs-terminal categories get their own member here.
    """

    NONE = ""
    PROVISION = "provision"


@dataclass(frozen=True, slots=True)
class JobBuildSource:
    """Bundle of :class:`FirmwareJob` ``source_*`` dispatch-origin fields."""

    source: JobSource = JobSource.LOCAL
    source_pin_sha256: str = ""
    source_label: str = ""
    source_esphome_version: str = ""

    @classmethod
    def for_server(cls, *, pin_sha256: str, label: str, esphome_version: str) -> JobBuildSource:
        """Bundle a REMOTE source bound to build server *pin_sha256*."""
        return cls(
            source=JobSource.REMOTE,
            source_pin_sha256=pin_sha256,
            source_label=label,
            source_esphome_version=esphome_version,
        )


# The wire value ``FirmwareJob.port`` carries for an over-the-air flash —
# the esphome CLI resolves the device's address itself.
OTA_PORT = "OTA"

LOCAL_JOB_BUILD_SOURCE = JobBuildSource()
# Submit-time marker for "remote-eligible, server chosen at dispatch".
# The pin/label/version stay empty until the dispatch pool resolves them.
REMOTE_PENDING_JOB_BUILD_SOURCE = JobBuildSource(source=JobSource.REMOTE_PENDING)


# Terminal job states — a job in any of these isn't running and
# isn't waiting to run.
TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)

# Active job states — queued or running (the complement of terminal).
_ACTIVE_JOB_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})

# Lifecycle events that match ``TERMINAL_JOB_STATUSES``. The runner
# fires exactly one of these per job, matching the status set
# above — kept as a separate constant because subscriptions key
# off ``EventType`` while state checks key off ``JobStatus``.
TERMINAL_JOB_EVENTS: frozenset[EventType] = frozenset(
    {EventType.JOB_COMPLETED, EventType.JOB_FAILED, EventType.JOB_CANCELLED}
)


@dataclass
class FirmwareJob(DashboardModel):
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
    # UPLOAD flashes the bootloader image instead of the app
    # (``esphome upload --bootloader``); OTA-only.
    flash_bootloader: bool = False
    # In-memory decision input for the deferred-install completion hook;
    # the durable arm is ``Device.queued_update``.
    is_deferred_install: bool = False
    # New device name for ``rename`` jobs. Plumbed through to the
    # ``esphome rename`` CLI. Empty for every other job type.
    new_name: str = ""
    # job_id of a prerequisite job that must complete successfully before
    # this one runs; empty for independent jobs. Set on the UPLOAD half of
    # an install chain (``depends_on`` the COMPILE job): the upload is held
    # off its lane queue until the compile succeeds, and cancelled if the
    # compile fails/cancels. Rides through ``JobLifecycleData`` so the
    # frontend can render the dependency.
    depends_on: str = ""
    # Latched once released onto its lane; a restart then routes it even if
    # the prerequisite was pruned. False without ``depends_on``.
    dependency_released: bool = False
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
    # Receiver's ``esphome.const.__version__`` at job-creation
    # time, snapshotted from :attr:`StoredPairing.esphome_version`.
    # Empty for ``LOCAL`` jobs and for ``REMOTE`` jobs whose
    # pairing hadn't yet completed a peer-link session (the
    # pairing field populates on every session-open).
    source_esphome_version: str = ""
    # Receiver-side: the offloader's esphome version off the
    # SUBMIT_JOB header. When set and it differs from the
    # receiver's installed esphome, the receiver provisions a
    # matching venv and compiles the job with it. Empty for
    # local jobs and older offloaders (compile with installed).
    target_esphome_version: str = ""
    # Machine-readable failure category on a FAILED terminal (see
    # :class:`JobFailureReason`), distinct from ``error`` (the human
    # message). ``PROVISION`` tells the offloader to rebuild locally.
    failure_reason: JobFailureReason = JobFailureReason.NONE

    @property
    def is_terminal(self) -> bool:
        """Whether the job has reached a terminal status (completed / failed / cancelled)."""
        return self.status in TERMINAL_JOB_STATUSES

    @property
    def is_active(self) -> bool:
        """Whether the job is still queued or running (not yet terminal)."""
        return self.status in _ACTIVE_JOB_STATUSES

    @property
    def is_rename_tail(self) -> bool:
        """
        Whether this is a rename chain's flash-and-swap tail.

        False for a fused ``esphome rename`` job (no ``depends_on``).
        """
        return self.job_type is JobType.RENAME and bool(self.depends_on)

    @property
    def is_network_flash(self) -> bool:
        """Whether this job flashes over the network (UPLOAD, or a rename tail)."""
        return self.job_type is JobType.UPLOAD or self.is_rename_tail

    @property
    def new_filename(self) -> str:
        """The YAML filename a rename's ``new_name`` resolves to."""
        return f"{self.new_name}.yaml"

    @property
    def flash_configuration(self) -> str:
        """The YAML whose build artifacts this job flashes (the renamed file for a tail)."""
        return self.new_filename if self.is_rename_tail else self.configuration

    @property
    def is_completed_ota_upload(self) -> bool:
        """Whether this is an OTA upload job that completed.

        Scoped to OTA specifically — a server-serial upload
        shouldn't trip the offline-queue machinery just because the
        device happens to also have ``queued_update`` set for an
        unrelated reason.
        """
        return (
            self.job_type == JobType.UPLOAD
            and self.port == OTA_PORT
            and self.status == JobStatus.COMPLETED
        )

    @property
    def is_deferred_compile_success(self) -> bool:
        """Whether this is a successfully-completed COMPILE from the offline-install path.

        Only a job queued via that path (``is_deferred_install``) that
        actually finished should trigger the "device woke up, flash
        now" follow-up — a plain compile, or one that failed, has
        nothing to act on.
        """
        return (
            self.job_type == JobType.COMPILE
            and self.status == JobStatus.COMPLETED
            and self.is_deferred_install
        )

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
          where the rebuild starts. (Re-routing a compile to
          another build server uses :meth:`clear_run_state`
          instead, so it doesn't claim a restart that didn't
          happen.)
        - **Clears per-run state** — ``progress`` / ``error`` /
          ``started_at`` / ``completed_at`` / ``exit_code``
          back to their defaults.
        - **Doesn't change ``status``** — the caller decides
          the transition (load path flips ``RUNNING`` →
          ``QUEUED``; future callers might want a different
          target).
        - **Preserves identity** — ``configuration`` /
          ``job_type`` / ``port`` / ``new_name`` / ``depends_on`` / ``created_at``
          / ``job_id`` / ``source`` / ``source_pin_sha256`` /
          ``source_label`` / ``source_esphome_version`` /
          ``remote_peer`` / ``remote_peer_label`` /
          ``remote_job_id`` / ``device_name`` /
          ``device_friendly_name`` describe the job rather than
          the run, so they stay
          intact.
        """
        self.output = [*self.output, _RECOVERY_NOTICE]
        self.clear_run_state()

    def mark_running(self) -> None:
        """Stamp this job RUNNING with the current start time."""
        self.status = JobStatus.RUNNING
        self.started_at = _now_iso()

    def mark_terminal(self, status: JobStatus, *, error: str | None = None) -> None:
        """Set a terminal *status* (and *error* if given) and stamp completion time.

        Raises ``ValueError`` on a non-terminal *status* so a stray call can't
        stamp ``completed_at`` on a still-running job (which mis-orders the
        dashboard's relative-time strings and confuses prune-on-shutdown).
        """
        if status not in TERMINAL_JOB_STATUSES:
            msg = f"mark_terminal called with non-terminal status {status!r}"
            raise ValueError(msg)
        if error is not None:
            self.error = error
        self.status = status
        self.completed_at = _now_iso()

    def revert_to_pending_remote(self) -> None:
        """Reset run state and re-mark ``REMOTE_PENDING`` so the pool re-routes this compile."""
        self.clear_run_state()
        self.status = JobStatus.QUEUED
        self.apply_build_source(REMOTE_PENDING_JOB_BUILD_SOURCE)

    def restore_for_requeue(self) -> None:
        """Prepare a restored active job for re-queue after a dashboard restart.

        A RUNNING job gets a fresh run via ``reset`` (restart marker); a remote
        COMPILE re-enters ``REMOTE_PENDING`` with its pin cleared so the dispatch
        pool re-routes it to a live server, while a REMOTE CLEAN keeps its pin
        (it targets a specific server on purpose).
        """
        if self.status is JobStatus.RUNNING:
            self.reset()
        self.status = JobStatus.QUEUED
        if self.job_type is JobType.COMPILE and self.source in (
            JobSource.REMOTE,
            JobSource.REMOTE_PENDING,
        ):
            self.apply_build_source(REMOTE_PENDING_JOB_BUILD_SOURCE)

    def clear_run_state(self) -> None:
        """Clear per-run fields (progress / error / timing / exit code); keeps output and identity.

        ``reset`` is this plus a restart marker; a mid-build re-route to
        another server calls this directly so the log isn't stamped with a
        restart notice that never happened.
        """
        self.progress = None
        self.error = None
        self.failure_reason = JobFailureReason.NONE
        self.started_at = None
        self.completed_at = None
        self.exit_code = None

    def apply_build_source(self, build_source: JobBuildSource) -> None:
        """Stamp this job's dispatch origin (source + pin / label / version) from *build_source*."""
        self.source = build_source.source
        self.source_pin_sha256 = build_source.source_pin_sha256
        self.source_label = build_source.source_label
        self.source_esphome_version = build_source.source_esphome_version


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
