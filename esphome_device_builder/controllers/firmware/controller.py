"""
Firmware build queue + WS command surface.

Owns the persistent single-job queue, the subprocess spawn loop,
mid-flight output capping, progress detection, and the lifecycle
event broadcasts. Public API is the ``@api_command``-decorated
methods; everything else is private. Pure data and free helpers
live in ``constants.py`` and ``helpers.py``.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import importlib
import logging
import os
import shutil
import sys
from contextlib import suppress
from datetime import UTC, datetime
from operator import attrgetter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from esphome.components.esp32 import VARIANTS as ESP32_VARIANTS
from esphome.components.libretiny.const import (
    FAMILY_COMPONENT as _LIBRETINY_FAMILY_COMPONENT,
)
from esphome.storage_json import StorageJSON, ext_storage_path

from ...helpers.api import CommandError, api_command
from ...helpers.event_bus import StreamControls, stream_events
from ...helpers.process import terminate_subtree_with_grace
from ...helpers.subprocess import create_subprocess_exec, iter_lines_with_progress
from ...models import ErrorCode, EventType, FirmwareJob, JobStatus, JobType, StreamEvent
from ..config import _load_metadata, metadata_transaction
from .constants import (
    _ERROR_PATTERNS,
    _INFLIGHT_TRIM_KEEP,
    _JOB_TERMINAL_EVENTS,
    _JOBS_KEY,
    _MAX_AUX_TERMINAL_JOBS,
    _MAX_OUTPUT_LINES_INFLIGHT,
    _MAX_PRIMARY_TERMINAL_JOBS,
    _PRIMARY_JOB_TYPES,
    _RESET_BUILD_ENV_TARGETS,
    _TERMINAL_JOB_STATUSES,
)
from .helpers import (
    _find_esphome_cmd,
    _is_no_module_named_esphome,
    _mark_job_terminal,
    _names_touched_by_job,
    _parse_progress,
    _trim_job_output,
    _validate_port,
    _verify_esphome_importable,
)

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.event_bus import Event

_LOGGER = logging.getLogger(__name__)

# Platforms whose ``target_platform`` value isn't the component
# module name. The dashboard download endpoint needs the
# ``esphome.components.<X>`` module that exposes
# ``get_download_types(storage)`` — for ESP32 variants that's the
# umbrella ``esp32`` component, and for LibreTiny chip families it's
# the ``libretiny`` component.
#
# The LibreTiny set is derived from upstream's
# ``FAMILY_COMPONENT.values()`` (auto-generated from
# ``generate_components.py``) so when LibreTiny adds a new chip
# family / component our mapping picks it up on the next
# ``esphome`` dependency bump — no edit here. The literal
# ``"libretiny"`` covers configs that report the umbrella name as
# ``target_platform`` directly.
#
# Mirrors ``esphome/dashboard/web_server.py``'s
# ``DownloadListRequestHandler`` — same shape, but driven by an
# upstream-sourced set rather than an inline literal.
_LIBRETINY_TARGET_PLATFORMS: frozenset[str] = frozenset(_LIBRETINY_FAMILY_COMPONENT.values()) | {
    "libretiny"
}


def _resolve_download_component(target_platform: str | None) -> str:
    """Return the ``esphome.components`` module name for *target_platform*.

    Accepts ``None`` so callers can pass ``StorageJSON.target_platform``
    (which is itself nullable) without an explicit ``or ""``
    coercion at the call site. Returns the empty string for empty
    / missing input — the caller's ``importlib.import_module`` will
    fail in its ``try/except`` block and log a warning.

    See ``_LIBRETINY_TARGET_PLATFORMS`` for the keep-in-sync note.
    """
    platform = (target_platform or "").lower()
    if platform.upper() in ESP32_VARIANTS:
        return "esp32"
    if platform in _LIBRETINY_TARGET_PLATFORMS:
        return "libretiny"
    return platform


class FirmwareController:
    """
    Manage firmware build jobs with a persistent queue.

    Only one job runs at a time. Jobs are persisted to disk so they
    survive page refreshes and server restarts. Progress is broadcast
    via the event bus to all connected clients.
    """

    def __init__(self, device_builder: DeviceBuilder) -> None:
        self._db = device_builder
        self._queue: asyncio.Queue[FirmwareJob] = asyncio.Queue()
        self._jobs: dict[str, FirmwareJob] = {}
        self._current_job: FirmwareJob | None = None
        self._current_process: asyncio.subprocess.Process | None = None
        self._runner_task: asyncio.Task | None = None
        self._esphome_cmd: list[str] = []
        # Job ids the user asked to cancel. Consulted by the runner
        # when the subprocess exits so we can mark the job CANCELLED
        # rather than the default FAILED-on-non-zero-exit.
        self._cancel_requested: set[str] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the queue processor and restore persisted jobs."""
        self._esphome_cmd = _find_esphome_cmd()
        _LOGGER.info(
            "ESPHome command: %s (interpreter: %s)",
            " ".join(self._esphome_cmd),
            sys.executable,
        )
        ok, detail = await _verify_esphome_importable(self._esphome_cmd)
        if ok:
            _LOGGER.info("ESPHome CLI sanity check OK — %s", detail)
        else:
            _LOGGER.error(
                "ESPHome CLI sanity check FAILED — %s. Compile/upload jobs "
                "will fail with this command. Make sure esphome is installed "
                "in the same environment as the dashboard "
                "(e.g. ``pip install -e '.[esphome]'`` from the project root).",
                detail,
            )
        await self._load_jobs()
        self._runner_task = self._db.create_background_task(self._run_queue())

    # ------------------------------------------------------------------
    # API commands — job submission
    # ------------------------------------------------------------------

    @api_command("firmware/compile")
    async def compile(self, *, configuration: str, **kwargs: Any) -> FirmwareJob:
        """Queue a compile job."""
        await self._validate_configuration_boundary(configuration)
        job = self._create_job(configuration, JobType.COMPILE)
        return await self._enqueue(job)

    @api_command("firmware/upload")
    async def upload(self, *, configuration: str, port: str = "", **kwargs: Any) -> FirmwareJob:
        """Queue an upload job.

        ``port`` is forwarded to the esphome CLI via ``--device``.
        Accepts:

        * ``"OTA"`` — let the CLI resolve the configured device's
          address from the YAML's ``esphome.address``.
        * A serial path (``/dev/ttyUSB0``, ``COM3``) — wired flash.
        * An IPv4 / IPv6 address or ``.local`` hostname — explicit
          OTA target. Useful for "install to a specific address"
          flows (re-flashing a device whose address has drifted, or
          flashing a known-good IP when mDNS is broken). The address
          cache is bypassed since the user has named the target
          explicitly.
        """
        _validate_port(port)
        await self._validate_configuration_boundary(configuration)
        job = self._create_job(configuration, JobType.UPLOAD, port=port)
        return await self._enqueue(job)

    @api_command("firmware/clean")
    async def clean(self, *, configuration: str, **kwargs: Any) -> FirmwareJob:
        """Queue a build clean job."""
        await self._validate_configuration_boundary(configuration)
        job = self._create_job(configuration, JobType.CLEAN)
        return await self._enqueue(job)

    @api_command("firmware/reset_build_env")
    async def reset_build_env(self, **kwargs: Any) -> FirmwareJob:
        """
        Queue a full reset of the build environment.

        Wipes per-device build outputs, external component checkouts,
        and the PlatformIO download cache. The next compile re-fetches
        external components and re-downloads toolchains from scratch
        — slow to recover from but the most thorough way to escape a
        poisoned cache. Runs through the same single-job queue as
        compile/upload so it can't race a build in progress.
        """
        job = self._create_job("", JobType.RESET_BUILD_ENV)
        return await self._enqueue(job)

    @api_command("firmware/install")
    async def install(self, *, configuration: str, port: str = "OTA", **kwargs: Any) -> FirmwareJob:
        """Queue a device update (compile + upload).

        ``port`` defaults to ``"OTA"`` — the CLI resolves the
        configured device's address from the YAML's
        ``esphome.address``. Accepts the same values as
        :meth:`upload`: a serial path for wired flashing, or an
        explicit IP / hostname for "install to a specific address"
        — the address cache is bypassed when the user names the
        target directly.
        """
        _validate_port(port)
        await self._validate_configuration_boundary(configuration)
        job = self._create_job(configuration, JobType.INSTALL, port=port)
        return await self._enqueue(job)

    @api_command("firmware/rename")
    async def rename(self, *, configuration: str, new_name: str, **kwargs: Any) -> FirmwareJob:
        """Queue a rename: compile + OTA-install the new firmware.

        Atomically swap the YAML on the dashboard once the install
        succeeds.

        Routed through the same single-job queue so it can't race a
        compile or install — and so it appears in the firmware-tasks
        list with live output instead of running silently in the
        background as it used to. ``esphome rename`` itself is
        responsible for keeping the old YAML around until the install
        succeeds; if the install fails the CLI rolls back the
        new-YAML write and the user can retry against the unchanged
        old hostname.
        """
        await self._validate_configuration_boundary(configuration)
        # ``new_name`` becomes ``<new_name>.yaml`` in config_dir; validate
        # the derived filename via ``rel_path`` at the WS boundary so a
        # direct ``firmware/rename`` request can't pass a traversal-shaped
        # name (``../etc/passwd``) and have it surface as a failed job
        # later.
        new_filename = f"{new_name}.yaml"
        await self._validate_configuration_boundary(new_filename)
        # Reject same-name renames up-front: the operation is a no-op
        # at the YAML level but still queues a real ``esphome rename``
        # job that re-compiles and OTA-flashes the device, so the
        # waste is real. Force the caller to use ``firmware/install``
        # instead — that's what they actually want.
        if new_filename == configuration:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                "new_name must differ from the current device name",
            )
        # Reject up-front if the target filename is already in use.
        # ``DevicesController.rename_device`` checks the same thing
        # before forwarding to this handler — but a direct WS client
        # can bypass that layer, and ``esphome rename`` itself does
        # not check collisions: it blindly ``write_text``s the new
        # YAML and OTA-installs it, silently overwriting the unrelated
        # device's config and flashing that firmware to the wrong
        # device. Same error-message shape as the controller-layer
        # check so the frontend handles both identically.
        # ``new_filename`` already passed ``rel_path`` validation
        # above, so we can build the path directly and stat it in
        # one executor hop instead of paying a second ``rel_path``
        # round-trip just to get back the same result.
        new_path = self._db.settings.config_dir / new_filename
        loop = asyncio.get_running_loop()
        if await loop.run_in_executor(None, new_path.exists):
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"A device named {new_filename} already exists",
            )
        job = self._create_job(configuration, JobType.RENAME, new_name=new_name)
        return await self._enqueue(job)

    @api_command("firmware/compile_bulk")
    async def compile_bulk(self, *, configurations: list[str], **kwargs: Any) -> list[FirmwareJob]:
        """Queue compile for multiple devices.

        Per-device errors (most commonly the rename lock) skip that
        device and keep going so a single locked configuration in a
        bulk request doesn't abort the queue for everyone else.
        """
        await self._validate_configurations_boundary(configurations)
        jobs: list[FirmwareJob] = []
        for config in configurations:
            try:
                job = self._create_job(config, JobType.COMPILE)
                await self._enqueue(job)
            except CommandError as exc:
                _LOGGER.info("Skipping %s in compile_bulk: %s", config, exc.message)
                continue
            jobs.append(job)
        return jobs

    @api_command("firmware/install_bulk")
    async def install_bulk(
        self, *, configurations: list[str], port: str = "OTA", **kwargs: Any
    ) -> list[FirmwareJob]:
        """Queue update (compile + upload) for multiple devices. Defaults to OTA.

        ``port`` is shared across every queued job; pass an explicit
        IP only when you really want every device installed against
        the same target (rare — almost always callers want the
        per-device default of ``"OTA"``).

        Per-device errors (most commonly the rename lock) skip that
        device and keep going — a rename-in-flight on one of the
        selected devices shouldn't abort the install for the rest.
        """
        _validate_port(port)
        await self._validate_configurations_boundary(configurations)
        jobs: list[FirmwareJob] = []
        for config in configurations:
            try:
                job = self._create_job(config, JobType.INSTALL, port=port)
                await self._enqueue(job)
            except CommandError as exc:
                _LOGGER.info("Skipping %s in install_bulk: %s", config, exc.message)
                continue
            jobs.append(job)
        return jobs

    # ------------------------------------------------------------------
    # API commands — job inspection
    # ------------------------------------------------------------------

    @api_command("firmware/get_jobs")
    async def get_jobs(
        self,
        *,
        status: JobStatus | str | None = None,
        configuration: str | None = None,
        **kwargs: Any,
    ) -> list[FirmwareJob]:
        """List jobs, optionally filtered by status or configuration."""
        jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        if configuration:
            jobs = [j for j in jobs if j.configuration == configuration]
        return sorted(jobs, key=attrgetter("created_at"), reverse=True)

    @api_command("firmware/get_job")
    async def get_job(self, *, job_id: str, **kwargs: Any) -> FirmwareJob | None:
        """Get a specific job with full output."""
        return self._jobs.get(job_id)

    @api_command("firmware/follow_job")
    async def follow_job(
        self, *, job_id: str, client: Any = None, message_id: str = "", **kwargs: Any
    ) -> None:
        """
        Follow a job's output: send historical lines then stream new ones.

        Behaves like ``tail -f`` with history. If the job is already
        finished, sends all output and a final result event.

        Race-free against the streaming loop: ``stream_events``
        subscribes to ``JOB_OUTPUT`` *before* the snapshot is sent,
        so the streaming loop cannot append between the snapshot
        capture and the subscription. Without that ordering, the
        previous shape iterated ``job.output`` directly and only
        subscribed afterwards, which had two failure modes:

        1. Lines appended to ``job.output`` during the history send
           (each ``send_event`` await yields the loop) fired a
           ``JOB_OUTPUT`` event with no subscriber attached and were
           dropped for this follower.
        2. The in-flight cap's ``_trim_job_output`` reassigns
           ``job.output`` to a new list, so an iteration over the
           old list reference stops seeing post-trim appends — making
           the gap above strictly bigger after every cap-crossing.

        Both failure modes are closed by snapshotting *before*
        ``stream_events`` runs and replaying inside ``send_initial``
        — every line fired after that point queues through the
        listener and lands strictly after history.
        """
        job = self._jobs.get(job_id)
        if not job:
            msg = f"Job not found: {job_id}"
            raise ValueError(msg)

        # Capture snapshot before stream_events attaches listeners.
        # The listener (attached inside stream_events) catches every
        # line fired after this point; nothing fires between the
        # snapshot and the subscribe because both happen in
        # synchronous-adjacent statements (stream_events' setup is
        # sync up to the first ``await`` inside ``send_initial``).
        snapshot = list(job.output)
        is_terminal = job.status in _TERMINAL_JOB_STATUSES
        terminal_status = job.status.value if is_terminal else ""
        terminal_exit_code = job.exit_code

        async def _send_initial(controls: StreamControls) -> None:
            for line in snapshot:
                await client.send_event(message_id, StreamEvent.OUTPUT, line)
            if is_terminal:
                await client.send_event(
                    message_id,
                    StreamEvent.RESULT,
                    {"status": terminal_status, "exit_code": terminal_exit_code},
                )
                # No live drain — already-terminal job has nothing
                # more to deliver; end the stream so the helper
                # returns instead of parking on ``queue.get``.
                controls.end()

        def _handle_event(event: Event, controls: StreamControls) -> None:
            if event.event_type == EventType.JOB_OUTPUT:
                if event.data.get("job_id") == job_id:
                    controls.push(StreamEvent.OUTPUT, event.data["line"])
            elif event.event_type in _JOB_TERMINAL_EVENTS:
                ev_job = event.data.get("job")
                if ev_job and getattr(ev_job, "job_id", None) == job_id:
                    status = getattr(ev_job, "status", "unknown")
                    status_val = status.value if hasattr(status, "value") else str(status)
                    controls.push_priority(
                        StreamEvent.RESULT,
                        {
                            "status": status_val,
                            "exit_code": getattr(ev_job, "exit_code", None),
                        },
                    )
                    controls.end()

        await stream_events(
            client=client,
            message_id=message_id,
            bus=self._db.bus,
            event_types=(EventType.JOB_OUTPUT, *_JOB_TERMINAL_EVENTS),
            handle_event=_handle_event,
            send_initial=_send_initial,
        )

    @api_command("firmware/follow_jobs")
    async def follow_jobs(
        self,
        *,
        client: Any = None,
        message_id: str = "",
        snapshot: bool = True,
        **kwargs: Any,
    ) -> None:
        """
        Stream every job's lifecycle events to one client connection.

        Designed for a "manage compile tasks" panel: subscribe once
        and the frontend sees every queued / started / progress /
        completed / failed / cancelled event for every job, plus
        live ``output`` lines tagged with their ``job_id``.

        When ``snapshot`` is True (default), the controller's full
        retained set of jobs — both active and the trimmed terminal
        history — is replayed first so the panel paints the complete
        picture immediately after a page refresh, with no extra round
        trip to ``firmware/get_jobs``. Each event keeps the same
        ``job`` payload shape as the bus, so the frontend can update
        its in-memory map by ``job_id`` without extra queries.

        Runs until the client disconnects (which surfaces here as a
        ``CancelledError`` from ``send_event``).

        Race-free against concurrent jobs the same way ``follow_job``
        is: ``stream_events`` attaches listeners *before* the
        snapshot replay is awaited, so a ``JOB_*`` event firing
        during the snapshot loop queues through the listener
        instead of being lost. The earlier shape sent the snapshot
        first and only attached listeners afterwards, so a job
        completing mid-replay silently disappeared from the stream.
        """
        if client is None:
            return

        # Serialize the snapshot to dicts synchronously *before*
        # ``stream_events`` attaches listeners. Capturing the
        # ``FirmwareJob`` objects and calling ``to_dict()`` later
        # (inside ``send_initial``) is racy: between listener
        # attach and each ``to_dict()`` the runner can append to a
        # running job's ``output`` or transition its status — that
        # mutation is folded into the snapshot dict AND delivered
        # again via the listener, so the client sees the same line
        # twice. Dict-freeze here makes the snapshot atomic against
        # the producer (no awaits between freeze and listener
        # attach) and de-duplicates the handoff.
        snapshot_payloads = (
            [job.to_dict() for job in sorted(self._jobs.values(), key=attrgetter("created_at"))]
            if snapshot
            else []
        )

        async def _send_initial(_controls: StreamControls) -> None:
            for payload in snapshot_payloads:
                await client.send_event(message_id, StreamEvent.SNAPSHOT, payload)

        def _handle_event(event: Event, controls: StreamControls) -> None:
            if event.event_type == EventType.JOB_OUTPUT:
                # Forward the bus event name through verbatim — the
                # all-jobs follower's wire protocol matches the
                # ``EventType`` value byte-for-byte for these high-
                # rate events. Pass the StrEnum member directly;
                # ``StreamControls.push`` accepts any str.
                controls.push(EventType.JOB_OUTPUT, event.data)
            elif event.event_type == EventType.JOB_PROGRESS:
                controls.push(EventType.JOB_PROGRESS, event.data)
            else:
                # Lifecycle event (queued/started/completed/failed/
                # cancelled). Use ``push_priority`` so a backlog of
                # ``job_output`` lines can't drop a status
                # transition — a missed ``job_completed`` would
                # leave the all-jobs panel stuck on the old status
                # forever (no resync after the initial snapshot).
                # Output/progress are tolerable to lose; status
                # transitions are not.
                job = event.data.get("job")
                if job is None:
                    return
                payload = job.to_dict() if hasattr(job, "to_dict") else job
                controls.push_priority(event.event_type.value, payload)

        await stream_events(
            client=client,
            message_id=message_id,
            bus=self._db.bus,
            event_types=(
                EventType.JOB_QUEUED,
                EventType.JOB_STARTED,
                *_JOB_TERMINAL_EVENTS,
                EventType.JOB_OUTPUT,
                EventType.JOB_PROGRESS,
            ),
            handle_event=_handle_event,
            send_initial=_send_initial,
        )

    @api_command("firmware/cancel")
    async def cancel(self, *, job_id: str, **kwargs: Any) -> None:
        """Cancel a queued or running job.

        Queued jobs are flipped to ``CANCELLED`` immediately. Running
        jobs receive a SIGTERM and are escalated to SIGKILL after a
        short grace period — the runner loop sees the dead process and
        finalises the job with status ``CANCELLED`` (instead of the
        usual ``FAILED`` for non-zero exits) thanks to the
        ``_cancel_requested`` flag set here.

        Either path fires ``JOB_CANCELLED`` on the bus so frontends
        following all-jobs streams stay consistent.

        User-facing rejections (unknown ``job_id``, already-terminal
        job) raise ``CommandError`` so the WS dispatcher surfaces
        the message verbatim. A bare ``ValueError`` would be wrapped
        as ``"Command failed: firmware/cancel"`` and the operator
        would lose the offending id / status. The state-out-of-sync
        case stays as ``RuntimeError`` — it's a server bug, not user
        input, and ``INTERNAL_ERROR`` is the right code.
        """
        job = self._jobs.get(job_id)
        if not job:
            msg = f"Job not found: {job_id}"
            raise CommandError(ErrorCode.NOT_FOUND, msg)

        if job.status == JobStatus.QUEUED:
            _mark_job_terminal(job, JobStatus.CANCELLED)
            self._prune_history()
            await self._persist_jobs()
            self._db.bus.fire(EventType.JOB_CANCELLED, {"job": job})
            return

        if job.status == JobStatus.RUNNING:
            if self._current_job is None or self._current_job.job_id != job_id:
                msg = "Running job is not the active subprocess (state out of sync)"
                raise RuntimeError(msg)
            self._cancel_requested.add(job_id)
            await self._terminate_current_process()
            return

        msg = f"Cannot cancel a {job.status.value} job"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)

    @api_command("firmware/clear")
    async def clear(self, *, status: JobStatus | str | None = None, **kwargs: Any) -> None:
        """
        Remove finished jobs from the list.

        If ``status`` is given, only remove jobs with that status.
        Otherwise removes completed, failed, and cancelled jobs.
        """
        terminal = _TERMINAL_JOB_STATUSES
        to_remove = [
            jid
            for jid, job in self._jobs.items()
            if (status and job.status == status) or (not status and job.status in terminal)
        ]
        for jid in to_remove:
            del self._jobs[jid]
        await self._persist_jobs()

    # ------------------------------------------------------------------
    # API commands — binary download
    # ------------------------------------------------------------------

    @api_command("firmware/get_binaries")
    async def get_binaries(self, *, configuration: str, **kwargs: Any) -> list[dict]:
        """
        List available firmware binaries for a compiled device.

        Returns ``[{title, file}]`` — the file names can be passed to
        ``firmware/download`` to retrieve the binary content.
        """
        # ``ext_storage_path`` resolves to ``<data_dir>/storage/...``
        # outside the config dir AND does no traversal sanitisation
        # of its own (upstream definition is just
        # ``CORE.data_dir / "storage" / f"{config_filename}.json"``),
        # so the validator below is the only gate that keeps a
        # traversal payload out of the inner closure. Do not reorder.
        await self._validate_configuration_boundary(configuration)
        loop = asyncio.get_running_loop()

        def _get_types() -> list[dict]:
            storage = StorageJSON.load(ext_storage_path(configuration))
            if storage is None:
                return []
            try:
                component = _resolve_download_component(storage.target_platform)
                module = importlib.import_module(f"esphome.components.{component}")
                return list(module.get_download_types(storage))
            except Exception:
                _LOGGER.warning("Could not determine download types for %s", configuration)
                return []

        return await loop.run_in_executor(None, _get_types)

    @api_command("firmware/download")
    async def download(
        self,
        *,
        configuration: str,
        file: str,
        compressed: bool = False,
        **kwargs: Any,
    ) -> dict:
        """
        Download a compiled firmware binary.

        Returns ``{filename, data, size, compressed}`` where ``data`` is
        base64-encoded bytes. For Web Serial flashing the frontend
        decodes the base64 itself.
        """
        # See ``get_binaries`` — ``ext_storage_path`` skips the config
        # dir entirely, so we re-validate at the WS boundary.
        # ``ext_storage_path`` itself does NOT path-sanitise — its
        # upstream definition is literally
        # ``CORE.data_dir / "storage" / f"{config_filename}.json"``,
        # so a traversal-shaped configuration would escape the
        # storage tree if it ever reached the inner closure. The
        # ``_validate_configuration_boundary`` line above is the only
        # gate; do not reorder. Coverage:
        # ``test_download.py::test_download_validator_runs_before_ext_storage_path``.
        await self._validate_configuration_boundary(configuration)
        loop = asyncio.get_running_loop()

        def _read_binary() -> dict:
            storage = StorageJSON.load(ext_storage_path(configuration))
            if storage is None or storage.firmware_bin_path is None:
                msg = "No firmware binary — compile the device first"
                raise FileNotFoundError(msg)

            base_dir = storage.firmware_bin_path.parent.resolve()
            path = (base_dir / file).resolve()
            # Path traversal protection
            path.relative_to(base_dir)

            if not path.is_file():
                msg = f"Binary not found: {file}"
                raise FileNotFoundError(msg)

            data = path.read_bytes()
            if compressed:
                data = gzip.compress(data, 9)

            filename = f"{storage.name}-{file}"
            if compressed:
                filename += ".gz"

            return {
                "filename": filename,
                "data": base64.b64encode(data).decode("ascii"),
                "size": len(data),
                "compressed": compressed,
            }

        return await loop.run_in_executor(None, _read_binary)

    # ------------------------------------------------------------------
    # Internals — queue processing
    # ------------------------------------------------------------------

    async def _run_queue(self) -> None:
        """Background loop: process one job at a time."""
        while True:
            job = await self._queue.get()
            if job.status == JobStatus.CANCELLED:
                continue
            await self._execute_job(job)

    async def _execute_job(self, job: FirmwareJob) -> None:  # noqa: PLR0912, PLR0915
        """Execute a single firmware job."""
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(UTC).isoformat()
        self._current_job = job
        _LOGGER.info(
            "Starting job %s: %s %s",
            job.job_id,
            job.job_type,
            job.configuration,
        )
        self._db.bus.fire(EventType.JOB_STARTED, {"job": job})
        await self._persist_jobs()

        try:
            # RESET_BUILD_ENV doesn't shell out — handle it inline.
            # Errors fall through to the existing except blocks below.
            if job.job_type == JobType.RESET_BUILD_ENV:
                await self._reset_build_env(job)
                return

            # Pre-flight: verify chip type for serial uploads
            if job.job_type in (JobType.UPLOAD, JobType.INSTALL):
                await self._verify_chip(job)

            # ``rel_path`` calls ``Path.resolve`` which does a sync
            # ``os.path.realpath`` — blocking the event loop. Push it
            # to the executor so the runner stays non-blocking
            # end-to-end (matters even for the runner because
            # ``bus.fire`` listeners are interleaved on the loop and
            # blocking here pauses every follower's event delivery).
            loop = asyncio.get_running_loop()
            config_path = str(
                await loop.run_in_executor(None, self._db.settings.rel_path, job.configuration)
            )
            cache_args = self._build_cache_args(job)
            cmd = self._build_command(job.job_type, config_path, job.port, cache_args, job.new_name)
            _LOGGER.debug("Running: %s", " ".join(cmd))

            # Force ANSI color output even though stdout isn't a TTY.
            # `PLATFORMIO_FORCE_ANSI` covers PlatformIO's own output;
            # `FORCE_COLOR` / `CLICOLOR_FORCE` cover everything that
            # uses click for output (esphome itself, esptool, etc.);
            # `PYTHONUNBUFFERED` keeps Python subprocesses flushing
            # progress lines (especially `\r`-terminated ones) instead
            # of buffering them until a `\n` arrives.
            env = {
                **os.environ,
                "PLATFORMIO_FORCE_ANSI": "true",
                "FORCE_COLOR": "1",
                "CLICOLOR_FORCE": "1",
                "PYTHONUNBUFFERED": "1",
            }
            proc = await create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                # Put the whole esphome → platformio → gcc tree in its
                # own process group so ``_terminate_current_process``
                # can signal the entire chain, not just the python
                # parent. Without this, killing the parent leaves the
                # compiler children orphaned and the build keeps
                # running until they finish on their own — exactly the
                # "stop compile doesn't work" symptom.
                start_new_session=True,
            )
            self._current_process = proc

            has_error_in_output = False
            # Captured at append time because the in-flight trim can
            # elide the offending line before the post-exit handler
            # runs. ``_check_error`` already had the line in hand
            # there; persisting the verdict here lets the post-exit
            # handler render a specific actionable message even
            # after a long noisy build trims the head.
            saw_no_esphome_module = False
            assert proc.stdout is not None  # type narrowing

            def _check_error(text: str) -> None:
                nonlocal has_error_in_output, saw_no_esphome_module
                if not saw_no_esphome_module and _is_no_module_named_esphome(text):
                    saw_no_esphome_module = True
                if has_error_in_output:
                    return
                for pattern in _ERROR_PATTERNS:
                    if pattern in text:
                        has_error_in_output = True
                        return

            def _check_progress(text: str) -> None:
                progress = _parse_progress(text)
                if progress is None:
                    return
                # Monotonic clamp — output sometimes flips between
                # phases (compile reports "100%", then flash starts at
                # "0%"). For a single coarse bar we want the highest
                # so far so the frontend doesn't appear to regress.
                current = job.progress or 0
                if progress > current:
                    job.progress = progress
                    self._db.bus.fire(
                        EventType.JOB_PROGRESS,
                        {"job_id": job.job_id, "progress": progress},
                    )

            # ``iter_lines_with_progress`` splits on `\n` _or_ `\r` so
            # carriage-return-based in-place updates (esptool's
            # `Writing at 0x... (5%)\r`, PlatformIO's progress bars)
            # survive the pipe instead of getting buffered until the
            # next newline. Each chunk keeps its trailing terminator
            # so the frontend can decide whether to append a new line
            # or overwrite the last one.
            async for line in iter_lines_with_progress(proc.stdout):
                job.output.append(line)
                # Bound mid-run memory growth. Without this, a build
                # that streams gigabytes of stderr (chatty
                # external_components fetch loop, esptool stuck on a
                # repeating error) holds every line in memory until
                # the subprocess exits — only the post-completion
                # ``_trim_job_output`` in the ``finally`` block ever
                # ran. Trim down to a smaller keep size than the
                # trigger so the next ``cap - keep`` appends don't
                # each pay an O(cap) slice copy. Concretely with the
                # current constants: cap=4000, keep=2000, so 2000
                # lines fit between trims.
                if len(job.output) > _MAX_OUTPUT_LINES_INFLIGHT:
                    _trim_job_output(job, keep=_INFLIGHT_TRIM_KEEP)
                self._db.bus.fire(
                    EventType.JOB_OUTPUT,
                    {"job_id": job.job_id, "line": line},
                )
                _check_error(line)
                _check_progress(line)

            exit_code = await proc.wait()
            job.exit_code = exit_code

            # If the user cancelled this job mid-run, the subprocess
            # exits non-zero (terminated by signal). Honour that
            # intent rather than reporting it as a generic failure.
            if job.job_id in self._cancel_requested:
                self._cancel_requested.discard(job.job_id)
                _mark_job_terminal(job, JobStatus.CANCELLED)
                self._db.bus.fire(EventType.JOB_CANCELLED, {"job": job})
                _LOGGER.info("Job %s cancelled mid-run (exit %s)", job.job_id, exit_code)
            else:
                success = exit_code == 0 and not has_error_in_output
                _mark_job_terminal(job, JobStatus.COMPLETED if success else JobStatus.FAILED)
                if has_error_in_output and exit_code == 0:
                    if saw_no_esphome_module:
                        job.error = (
                            "esphome is not importable from the dashboard's Python "
                            f"environment ({sys.executable}). Install it with "
                            "``pip install -e '.[esphome]'`` "
                            "(or ``pip install esphome``) "
                            "in the same venv and restart the dashboard."
                        )
                    else:
                        job.error = "Process exited 0 but output contains errors"
                    _LOGGER.warning("Job %s: %s", job.job_id, job.error)

                event = EventType.JOB_COMPLETED if success else EventType.JOB_FAILED
                self._db.bus.fire(event, {"job": job})
                _LOGGER.info(
                    "Job %s %s (exit code %s)",
                    job.job_id,
                    job.status,
                    exit_code,
                )

        except asyncio.CancelledError:
            if self._current_process:
                self._current_process.terminate()
            _mark_job_terminal(job, JobStatus.CANCELLED)
            self._cancel_requested.discard(job.job_id)
            self._db.bus.fire(EventType.JOB_CANCELLED, {"job": job})
            _LOGGER.info("Job %s cancelled (runner shutdown)", job.job_id)
            raise
        except Exception as exc:
            job.error = str(exc)
            _mark_job_terminal(job, JobStatus.FAILED)
            self._db.bus.fire(EventType.JOB_FAILED, {"job": job})
            _LOGGER.exception("Job %s failed: %s", job.job_id, exc)
        finally:
            self._current_job = None
            self._current_process = None
            if job.status in (
                JobStatus.COMPLETED,
                JobStatus.FAILED,
                JobStatus.CANCELLED,
            ):
                _trim_job_output(job)
                self._prune_history()
            await self._persist_jobs()

    async def _terminate_current_process(self) -> None:
        """Signal the running subprocess (and its children); escalate if it lingers.

        The runner loop is the one that actually finalises the
        ``FirmwareJob`` on exit (so we don't double-write status from
        two coroutines). We only nudge the process here.

        ESPHome forks PlatformIO which forks gcc / esptool / etc. The
        spawn site uses ``start_new_session=True`` (POSIX) so the whole
        tree shares a process group; we signal the group instead of
        just the python parent — without that, the compiler children
        get orphaned and the build keeps going until they finish.

        Windows has no process groups in the POSIX sense; we use
        ``taskkill /F /T`` to walk the parent-child tree from the
        kernel's accounting and force-kill the whole subtree in one
        shot. There's no graceful SIGTERM stage on Windows because the
        compile chain doesn't honour any of the polite signals.
        """
        proc = self._current_process
        if proc is None:
            return
        await terminate_subtree_with_grace(
            proc,
            job_label=f"job {self._current_job.job_id}" if self._current_job else "job ?",
        )

    async def _reset_build_env(self, job: FirmwareJob) -> None:
        """
        Run a ``RESET_BUILD_ENV`` job to completion or cancellation.

        Streams progress lines through the same ``JOB_OUTPUT`` event
        used by compile/upload jobs and finalises ``job.status``
        before returning. Mid-run cancellation is honoured between
        targets, not during a single ``rmtree``.
        """
        esphome_root = self._db.settings.config_dir / ".esphome"
        loop = asyncio.get_running_loop()

        def _emit(text: str) -> None:
            line = text if text.endswith("\n") else text + "\n"
            job.output.append(line)
            self._db.bus.fire(
                EventType.JOB_OUTPUT,
                {"job_id": job.job_id, "line": line},
            )

        _emit(f"Resetting build environment under {esphome_root}")

        # Batch every ``os.stat`` into one executor hop instead of N
        # separate ``run_in_executor(None, target.exists)`` awaits.
        # We can't run the whole function in an executor because the
        # per-step ``_emit`` fires ``bus.fire(JOB_OUTPUT, ...)`` and
        # listeners call ``asyncio.Queue.put_nowait`` (not thread-safe
        # off the loop), so live followers would corrupt their queues
        # if the emit ran from a worker thread. Probing all the stats
        # up front keeps emit + ``rmtree`` on the loop while still
        # being fully non-blocking. TOCTOU race against a concurrent
        # external delete is the same shape the original sequential
        # stat / rmtree had — ``rmtree`` would raise on a missing
        # target either way.
        def _probe() -> tuple[bool, dict[str, bool]]:
            if not esphome_root.exists():
                return False, {}
            return True, {name: (esphome_root / name).exists() for name in _RESET_BUILD_ENV_TARGETS}

        esphome_exists, target_exists = await loop.run_in_executor(None, _probe)

        if not esphome_exists:
            _emit("Nothing to do — .esphome/ does not exist yet.")
        else:
            for name in _RESET_BUILD_ENV_TARGETS:
                # rmtree isn't interruptible from another coroutine,
                # so we can only stop before starting the next target.
                if job.job_id in self._cancel_requested:
                    self._cancel_requested.discard(job.job_id)
                    _emit("Reset cancelled by user.")
                    _mark_job_terminal(job, JobStatus.CANCELLED)
                    self._db.bus.fire(EventType.JOB_CANCELLED, {"job": job})
                    return

                if not target_exists[name]:
                    _emit(f"  skipped (not present): {name}/")
                    continue
                _emit(f"  removing {name}/ ...")
                await loop.run_in_executor(None, shutil.rmtree, esphome_root / name)
                _emit(f"  removed {name}/")

        _emit(
            "Reset complete — the next compile will re-download "
            "toolchains and re-fetch external components."
        )
        job.exit_code = 0
        job.progress = 100
        _mark_job_terminal(job, JobStatus.COMPLETED)
        self._db.bus.fire(EventType.JOB_COMPLETED, {"job": job})
        _LOGGER.info("Job %s reset_build_env completed", job.job_id)

    async def _verify_chip(self, job: FirmwareJob) -> None:
        """
        Verify the chip on the serial port matches the device config.

        Runs ``esptool chip-id`` to detect the actual chip, then
        compares against the target platform in the device config.
        Raises ValueError on mismatch so the job fails early with a
        clear error message.
        """
        if not job.port or job.port.upper() == "OTA" or not job.port.startswith("/dev"):
            return  # only check serial ports

        device = None
        if self._db.devices:
            target_name = job.configuration.removesuffix(".yaml").removesuffix(".yml")
            device = next(
                (d for d in self._db.devices.get_devices() if d.name == target_name),
                None,
            )

        expected_platform = ""
        if device and device.target_platform:
            expected_platform = device.target_platform.lower()
        if not expected_platform:
            return  # can't verify without knowing expected platform

        proc = await create_subprocess_exec(
            sys.executable,
            "-m",
            "esptool",
            "--port",
            job.port,
            "chip-id",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None  # type narrowing
        output = (await proc.stdout.read()).decode("utf-8", errors="replace")
        await proc.wait()

        # Parse "Detecting chip type... ESP32-C3"
        detected = ""
        for line in output.splitlines():
            if "Detecting chip type" in line:
                detected = line.split("...")[-1].strip().lower().replace("-", "")
                break

        if not detected:
            _LOGGER.warning("Could not detect chip type on %s", job.port)
            return

        # Normalise: "esp32c3" matches "esp32c3", "esp32" matches "esp32".
        # The target_platform from StorageJSON might be "ESP32S3" (uppercase).
        expected_normalized = expected_platform.lower().replace("-", "").replace("_", "")
        detected_normalized = detected.replace(" ", "")

        if expected_normalized != detected_normalized:
            msg = (
                f"Chip mismatch: config expects {expected_platform} "
                f"but {job.port} has {detected}. Wrong board selected?"
            )
            raise ValueError(msg)

        _LOGGER.debug("Chip verified: %s on %s", detected, job.port)

    def _build_command(
        self,
        job_type: JobType,
        config_path: str,
        port: str,
        cache_args: list[str] | None = None,
        new_name: str = "",
    ) -> list[str]:
        """Build the esphome CLI command for a given job type."""
        cmd_map = {
            JobType.COMPILE: "compile",
            JobType.UPLOAD: "upload",
            JobType.INSTALL: "run",
            JobType.CLEAN: "clean",
            JobType.RENAME: "rename",
        }
        # cache_args go before the subcommand — esphome's argparse parses
        # them on the top-level parser, not the per-subcommand one.
        # ``--dashboard`` flips ESPHome's log formatter into "escape ANSI
        # as literal text" mode, which survives the colorama strip when
        # stdout is piped to us; the frontend's ansi-log component then
        # un-escapes and renders the colours.
        cmd = [
            *self._esphome_cmd,
            "--dashboard",
            *(cache_args or []),
            cmd_map[job_type],
            config_path,
        ]
        if job_type == JobType.INSTALL:
            # Without --no-logs the CLI tails logs forever after the
            # upload, never returning — the job would never complete.
            cmd.append("--no-logs")
        if job_type in (JobType.UPLOAD, JobType.INSTALL) and port:
            cmd.extend(["--device", port])
        if job_type == JobType.RENAME:
            # ``esphome rename`` takes the new name as a positional
            # arg. The CLI handles the inner compile + install + old
            # YAML cleanup itself; we let the queue runner stream its
            # output the same way it does for any other build.
            cmd.append(new_name)
        return cmd

    def _build_cache_args(self, job: FirmwareJob) -> list[str]:
        """Return ``--mdns/--dns-address-cache`` args for *job*, or empty."""
        # Only OTA uploads benefit — serial flashes don't talk to the
        # device's network address at all. ``rename`` does an internal
        # OTA install via ``esphome run`` against the *old* address, so
        # the same cache shortcut applies.
        if job.job_type not in (JobType.UPLOAD, JobType.INSTALL, JobType.RENAME):
            return []
        if job.job_type != JobType.RENAME and job.port != "OTA":
            return []
        if self._db.devices is None:
            return []
        return self._db.devices.get_address_cache_args(job.configuration)

    # ------------------------------------------------------------------
    # Internals — job management
    # ------------------------------------------------------------------

    def _sync_validate_configuration_boundary(self, configuration: str) -> None:
        """
        Run the synchronous ``rel_path`` check; raise ``CommandError`` on bad input.

        Used by both ``_validate_configuration_boundary`` (the async
        per-call wrapper) and ``_validate_configurations_boundary`` (which
        already runs inside an executor). Centralises the rule so
        future changes to validation logic land in exactly one place.

        Empty strings raise too — ``reset_build_env`` is the only code
        path that legitimately wants the empty configuration value, and
        it bypasses this validator entirely. Without this check a
        client could call ``firmware/compile`` with ``configuration=""``,
        get a queued job, and only fail later when ``_execute_job`` hands
        the empty string to the CLI.

        Callers must NOT invoke this directly from the event loop —
        ``rel_path`` calls ``Path.resolve``, a blocking
        ``os.path.abspath`` syscall that blockbuster catches on CI.
        """
        if not configuration:
            raise CommandError(ErrorCode.INVALID_ARGS, "configuration must not be empty")
        self._db.settings.rel_path(configuration)

    async def _validate_configuration_boundary(self, configuration: str) -> None:
        """
        Validate ``configuration`` inside an executor.

        Single-config path; ``CommandError(INVALID_ARGS)`` on traversal
        or empty input propagates through the awaited future to the
        WS dispatcher unchanged.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_validate_configuration_boundary, configuration)

    async def _validate_configurations_boundary(self, configurations: list[str]) -> None:
        """
        Validate every configuration in a single executor task; raise on bad input.

        One ``run_in_executor`` for the whole batch instead of N — the
        per-config ``rel_path`` call is cheap, but spinning up an
        executor task per config adds context-switch overhead that
        scales badly on a large bulk request.

        Bad input (traversal, empty) raises ``CommandError(INVALID_ARGS)``
        for the whole batch rather than silently dropping the entry —
        a typo in one of N configurations is something the caller wants
        to know about, not have masked by partial success. Transient
        state conflicts (rename-lock rejections) are still handled with
        skip-and-continue inside the bulk handlers' phase-2 loop;
        validation is the upfront gate, queue contention is the
        downstream best-effort step.
        """

        def _validate_all() -> None:
            for config in configurations:
                self._sync_validate_configuration_boundary(config)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _validate_all)

    def _create_job(
        self,
        configuration: str,
        job_type: JobType,
        port: str = "",
        new_name: str = "",
    ) -> FirmwareJob:
        """Create a new job and add it to the in-memory map.

        Caller is responsible for having validated ``configuration``
        first via ``_validate_configuration_boundary`` — keeping it
        async-only lets the validation run in an executor without
        making this helper async too.
        """
        job = FirmwareJob(
            job_id=uuid4().hex[:12],
            configuration=configuration,
            job_type=job_type,
            created_at=datetime.now(UTC).isoformat(),
            port=port,
            new_name=new_name,
        )
        self._jobs[job.job_id] = job
        return job

    async def _enqueue(self, job: FirmwareJob) -> FirmwareJob:
        """
        Enqueue a job, persist, and fire JOB_QUEUED.

        Cancels any queued or running job for the same device so the
        manage-tasks panel only shows one active job per device — a
        fresh compile/upload/install/clean request makes earlier
        in-flight work irrelevant. We fire ``JOB_QUEUED`` for the
        new job *before* cancelling the predecessor so frontends can
        recognise the resulting ``JOB_CANCELLED`` as a supersede
        (already-present successor for the same configuration) and
        drop the old entry silently rather than parking it in the
        "Recent" history. Reset jobs (empty configuration) skip the
        supersede.

        Rejects with ``CommandError(INVALID_ARGS)`` when an in-flight
        ``RENAME`` job has the new job's configuration locked. Rename
        rewrites the YAML mid-flight (old YAML still on disk during
        compile, new YAML only written on install success), so a
        compile/install/clean/upload — or another rename targeting the
        same old or new name — would fight for files the rename is
        actively reading or about to write. Same-old-config rename
        retries are allowed through so the supersede path can cancel
        and replace.
        """
        self._check_rename_lock(job)
        await self._queue.put(job)
        self._db.bus.fire(EventType.JOB_QUEUED, {"job": job})
        if job.configuration:
            await self._supersede_active_jobs(job.configuration, exclude_job_id=job.job_id)
        await self._persist_jobs()
        return job

    def _check_rename_lock(self, job: FirmwareJob) -> None:
        """Reject jobs that would clash with an in-flight rename.

        A rename touches two YAML filenames: the old one it's reading
        from and the new one it'll create on install success. Any
        other job that touches either name would either fight for the
        same file or land its work on a half-flashed device. The one
        exception is a fresh ``RENAME`` on the same old configuration
        — that's an explicit user retry / target-name change and the
        supersede path is meant to cancel-and-replace.
        """
        new_touches = _names_touched_by_job(job)
        if not new_touches:
            return
        for active in self._jobs.values():
            if active.job_type != JobType.RENAME:
                continue
            if active.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                continue
            # Same-old-config rename retry: let supersede do its thing.
            if job.job_type == JobType.RENAME and job.configuration == active.configuration:
                continue
            clash = new_touches & _names_touched_by_job(active)
            if not clash:
                continue
            old = active.configuration
            new = f"{active.new_name}.yaml" if active.new_name else "(unknown)"
            msg = (
                f"Device {old} is being renamed to {new}; wait for the "
                f"rename to finish before queueing another firmware "
                f"task on either name."
            )
            raise CommandError(ErrorCode.INVALID_ARGS, msg)

    async def _supersede_active_jobs(self, configuration: str, *, exclude_job_id: str) -> None:
        """Cancel queued/running jobs for ``configuration``."""
        to_cancel = [
            j.job_id
            for j in self._jobs.values()
            if j.job_id != exclude_job_id
            and j.configuration == configuration
            and j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
        ]
        for job_id in to_cancel:
            # Status may flip under us if the runner finalises the
            # job mid-iteration; cancel() raises in that window and
            # we don't care.
            with suppress(ValueError, RuntimeError):
                await self.cancel(job_id=job_id)

    def _prune_history(self) -> None:
        """
        Trim ``self._jobs`` to the configured history limits.

        Active (queued/running) jobs are always kept. Terminal
        compile/upload/install jobs collapse to one entry per
        configuration (newest wins) and are capped at
        ``_MAX_PRIMARY_TERMINAL_JOBS``. Terminal clean/reset jobs are
        kept in a separate pool capped at ``_MAX_AUX_TERMINAL_JOBS``.
        Caller persists the result.
        """
        terminal_states = _TERMINAL_JOB_STATUSES

        active: list[FirmwareJob] = []
        primary: list[FirmwareJob] = []
        aux: list[FirmwareJob] = []
        for job in self._jobs.values():
            if job.status not in terminal_states:
                active.append(job)
            elif job.job_type in _PRIMARY_JOB_TYPES:
                primary.append(job)
            else:
                aux.append(job)

        # Sort newest-first so dedup keeps the most recent entry per
        # device and the cap retains the most recent N overall.
        primary.sort(key=attrgetter("created_at"), reverse=True)
        seen_configs: set[str] = set()
        deduped_primary: list[FirmwareJob] = []
        for job in primary:
            if job.configuration:
                if job.configuration in seen_configs:
                    continue
                seen_configs.add(job.configuration)
            deduped_primary.append(job)
        deduped_primary = deduped_primary[:_MAX_PRIMARY_TERMINAL_JOBS]

        aux.sort(key=attrgetter("created_at"), reverse=True)
        aux = aux[:_MAX_AUX_TERMINAL_JOBS]

        self._jobs = {j.job_id: j for j in (*active, *deduped_primary, *aux)}

    # ------------------------------------------------------------------
    # Internals — persistence
    # ------------------------------------------------------------------

    async def _load_jobs(self) -> None:
        """
        Load persisted jobs and re-queue any incomplete ones.

        - ``QUEUED`` and ``RUNNING`` both re-queue. The user
          asked for the build; even though the subprocess died
          with the dashboard, the request is still pending in
          their head. Worst case the rebuilt-and-reflashed
          firmware is identical to what was already on the
          device — that's idempotent, the user pays a couple
          minutes of compile time, no harm done.
        - Terminal (``COMPLETED`` / ``FAILED`` / ``CANCELLED``):
          load into the in-memory map for the recent-jobs panel
          but don't touch ``_queue``.

        ``RUNNING`` jobs go through ``FirmwareJob.reset()``
        before being re-queued so the rebuild looks like a
        fresh run in the per-run-state fields (``progress`` /
        ``error`` / ``started_at`` / ``completed_at`` /
        ``exit_code``) but keeps the pre-crash ``output`` log
        as diagnostic history with a separator marker showing
        where the rebuild starts. ``reset`` lives on the model
        rather than as a free helper here so a future per-run
        field added to ``FirmwareJob`` lands right next to the
        method that has to clear it.

        See esphome/device-builder#147 for the policy discussion.
        """
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _load_metadata, self._db.settings.config_dir)
        for job_data in data.get(_JOBS_KEY, []):
            try:
                job = FirmwareJob.from_dict(job_data)
                self._jobs[job.job_id] = job
                if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                    if job.status == JobStatus.RUNNING:
                        job.reset()
                    job.status = JobStatus.QUEUED
                    await self._queue.put(job)
            except Exception:
                # ``job_data`` is normally a dict, but a corrupt
                # persistence file could contain a primitive (string,
                # int, ``None``) where a dict was expected. ``.get``
                # would raise ``AttributeError`` on those, defeating
                # the "skip and continue" intent of this branch.
                # Probe by isinstance and fall back to the raw repr.
                identity = (
                    job_data.get("job_id", "?")
                    if isinstance(job_data, dict)
                    else f"<non-dict entry: {job_data!r}>"
                )
                _LOGGER.warning("Failed to restore job: %s", identity, exc_info=True)

    async def _persist_jobs(self) -> None:
        """Save all jobs to disk."""
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir

        def _save() -> None:
            with metadata_transaction(config_dir) as data:
                data[_JOBS_KEY] = [j.to_dict() for j in self._jobs.values()]

        await loop.run_in_executor(None, _save)
