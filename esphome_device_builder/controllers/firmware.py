"""Firmware controller — build queue, compile, upload, validate, clean, download."""

from __future__ import annotations

import asyncio
import base64
import gzip
import importlib
import logging
import os
import re
import shutil
import subprocess
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from esphome.components.esp32 import VARIANTS as ESP32_VARIANTS
from esphome.storage_json import StorageJSON, ext_storage_path

from ..controllers.config import _load_metadata, metadata_transaction
from ..helpers.api import api_command
from ..models import EventType, FirmwareJob, JobStatus, JobType

if TYPE_CHECKING:
    from ..device_builder import DeviceBuilder

_LOGGER = logging.getLogger(__name__)
_JOBS_KEY = "_firmware_jobs"

# Output patterns that indicate failure even when the subprocess exit
# code is 0 (Python tracebacks routed through `print()`, etc.).
_ERROR_PATTERNS = [
    "ModuleNotFoundError",
    "ImportError",
    "No module named",
    "FileNotFoundError",
    "command not found",
]

# Progress markers in PlatformIO / esptool output. Both phases emit
# integer percentages we can latch onto:
#   - PIO compile:  ``[ 17%] Compiling ...``
#   - esptool:      ``Writing at 0x... (45 %)\r``
# We accept either form (with or without the space before ``%``) and
# pull the first match per line.
_PROGRESS_PATTERN = re.compile(r"\[?\s*(\d{1,3})\s*%\]?")

# How long to wait for a SIGTERM'd subprocess to exit before we
# escalate to SIGKILL. ESPHome / PlatformIO usually clean up promptly;
# the longer floor protects against esptool mid-flash where USB I/O
# can stall the process briefly.
_TERMINATE_GRACE_SECONDS = 3.0

# History retention. Bulk operations can spawn dozens of jobs at once;
# we want a useful audit trail without letting the metadata file grow
# without bound.
#   - "Primary" = COMPILE / UPLOAD / INSTALL: dedup'd to the most
#     recent terminal job per device, then capped globally.
#   - "Aux" = CLEAN / RESET_BUILD_ENV: kept in a separate small pool
#     so they don't crowd out the device history.
# Active (queued/running) jobs are exempt from both pools.
_MAX_PRIMARY_TERMINAL_JOBS = 50
_MAX_AUX_TERMINAL_JOBS = 5
_PRIMARY_JOB_TYPES: frozenset[JobType] = frozenset(
    {JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL}
)

# Per-job output cap for retained terminal jobs. Compile output for a
# successful build runs ~3-10k lines; the head is mostly toolchain
# noise that's rarely useful once the build finished. Live job output
# is unbounded — the cap kicks in only when the job lands in a
# terminal state.
_MAX_OUTPUT_LINES_RETAINED = 2000
_OUTPUT_TRIM_NOTICE_PREFIX = "... [output trimmed:"

# Subdirectories of ``<config_dir>/.esphome/`` that ``RESET_BUILD_ENV``
# wipes. Order is informational only — each is removed independently.
_RESET_BUILD_ENV_TARGETS = (
    "build",
    "external_components",
    "platformio_cache",
)


def _trim_job_output(job: FirmwareJob) -> None:
    """
    Cap ``job.output`` at the last ``_MAX_OUTPUT_LINES_RETAINED`` lines.

    Mutates the job in place. Safe to call repeatedly on the same
    job — already-trimmed output stays stable and the elided count
    keeps growing as new lines are dropped.
    """
    output = job.output
    extra_elided = 0
    # Recover and fold in the previous elided count so repeated trims
    # don't pretend only one line was dropped on each subsequent call.
    if output and output[0].startswith(_OUTPUT_TRIM_NOTICE_PREFIX):
        match = re.search(r"(\d+) earlier", output[0])
        if match:
            extra_elided = int(match.group(1))
        output = output[1:]
    if len(output) <= _MAX_OUTPUT_LINES_RETAINED:
        return
    new_elided = len(output) - _MAX_OUTPUT_LINES_RETAINED
    total_elided = extra_elided + new_elided
    job.output = [
        f"{_OUTPUT_TRIM_NOTICE_PREFIX} {total_elided} earlier line(s) elided]\n",
        *output[-_MAX_OUTPUT_LINES_RETAINED:],
    ]


def _find_esphome_cmd() -> list[str]:
    """Locate the ``esphome`` CLI, preferring the same interpreter as ours.

    The backend's own interpreter (``sys.executable``) is the
    authoritative source: if it can import ``esphome`` to start the
    server, it can run ``python -m esphome`` for compile jobs. We
    don't try to substitute a sibling ``python`` next to
    ``sys.executable`` — that's an easy way to silently jump to a
    different interpreter (e.g. a system Python without esphome
    installed) and produce confusing "No module named esphome"
    errors at compile time.

    A standalone ``esphome`` script in the *same* bin directory as
    our interpreter is preferred when present (slightly cheaper than
    ``python -m esphome`` and surfaces a friendlier traceback when
    something goes wrong inside esphome).
    """
    python = sys.executable
    bin_dir = Path(python).parent

    sibling_esphome = bin_dir / ("esphome.exe" if os.name == "nt" else "esphome")
    if sibling_esphome.exists():
        return [str(sibling_esphome)]

    return [python, "-m", "esphome"]


def _parse_progress(line: str) -> int | None:
    """Extract a 0-100 progress percentage from a build/flash output line.

    Returns ``None`` when the line carries no recognisable percentage.
    Out-of-range values (a stray "999%" in a log message) are
    discarded so the field stays a clean 0-100.
    """
    match = _PROGRESS_PATTERN.search(line)
    if match is None:
        return None
    value = int(match.group(1))
    if 0 <= value <= 100:
        return value
    return None


def _verify_esphome_importable(cmd: list[str]) -> tuple[bool, str]:
    """Sanity-check that ``cmd`` can actually import esphome.

    Runs ``cmd --version`` synchronously with a short timeout. Used at
    backend startup so misconfigured environments (venv missing
    esphome, wrong sys.executable, broken shim script) surface as a
    clear log line rather than a cryptic "No module named esphome"
    output captured during the user's first compile attempt.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — cmd is built from sys.executable, not user input
            [*cmd, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0 or "No module named" in output or "ModuleNotFoundError" in output:
        return False, output or f"exit {proc.returncode}"
    return True, output


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
        loop = asyncio.get_running_loop()
        ok, detail = await loop.run_in_executor(None, _verify_esphome_importable, self._esphome_cmd)
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
        job = self._create_job(configuration, JobType.COMPILE)
        return await self._enqueue(job)

    @api_command("firmware/upload")
    async def upload(self, *, configuration: str, port: str = "", **kwargs: Any) -> FirmwareJob:
        """Queue an upload job."""
        job = self._create_job(configuration, JobType.UPLOAD, port=port)
        return await self._enqueue(job)

    @api_command("firmware/clean")
    async def clean(self, *, configuration: str, **kwargs: Any) -> FirmwareJob:
        """Queue a build clean job."""
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
        """Queue a device update (compile + upload). Defaults to OTA."""
        job = self._create_job(configuration, JobType.INSTALL, port=port)
        return await self._enqueue(job)

    @api_command("firmware/compile_bulk")
    async def compile_bulk(self, *, configurations: list[str], **kwargs: Any) -> list[FirmwareJob]:
        """Queue compile for multiple devices."""
        jobs = []
        for config in configurations:
            job = self._create_job(config, JobType.COMPILE)
            await self._enqueue(job)
            jobs.append(job)
        return jobs

    @api_command("firmware/install_bulk")
    async def install_bulk(
        self, *, configurations: list[str], port: str = "OTA", **kwargs: Any
    ) -> list[FirmwareJob]:
        """Queue update (compile + upload) for multiple devices. Defaults to OTA."""
        jobs = []
        for config in configurations:
            job = self._create_job(config, JobType.INSTALL, port=port)
            await self._enqueue(job)
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
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

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
        """
        job = self._jobs.get(job_id)
        if not job:
            msg = f"Job not found: {job_id}"
            raise ValueError(msg)

        # Send historical output
        for line in job.output:
            await client.send_event(message_id, "output", line)

        # If already finished, send final status and return
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            await client.send_event(
                message_id,
                "result",
                {
                    "status": job.status.value,
                    "exit_code": job.exit_code,
                },
            )
            return

        # Subscribe to new output for this specific job
        done = asyncio.Event()
        pending_tasks: set[asyncio.Task] = set()

        def _on_event(event: Any) -> None:
            if event.event_type == EventType.JOB_OUTPUT:
                if event.data.get("job_id") == job_id:
                    task = asyncio.create_task(
                        client.send_event(message_id, "output", event.data["line"])
                    )
                    pending_tasks.add(task)
                    task.add_done_callback(pending_tasks.discard)
            elif event.event_type in (EventType.JOB_COMPLETED, EventType.JOB_FAILED):
                ev_job = event.data.get("job")
                if ev_job and getattr(ev_job, "job_id", None) == job_id:
                    status = getattr(ev_job, "status", "unknown")
                    status_val = status.value if hasattr(status, "value") else str(status)
                    task = asyncio.create_task(
                        client.send_event(
                            message_id,
                            "result",
                            {
                                "status": status_val,
                                "exit_code": getattr(ev_job, "exit_code", None),
                            },
                        )
                    )
                    pending_tasks.add(task)
                    task.add_done_callback(pending_tasks.discard)
                    done.set()

        unsub_output = self._db.bus.add_listener(EventType.JOB_OUTPUT, _on_event)
        unsub_completed = self._db.bus.add_listener(EventType.JOB_COMPLETED, _on_event)
        unsub_failed = self._db.bus.add_listener(EventType.JOB_FAILED, _on_event)

        try:
            await done.wait()
        finally:
            unsub_output()
            unsub_completed()
            unsub_failed()

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
        """
        if client is None:
            return

        if snapshot:
            for job in sorted(self._jobs.values(), key=lambda j: j.created_at):
                await client.send_event(message_id, "snapshot", job.to_dict())

        pending_tasks: set[asyncio.Task] = set()

        def _forward(event_name: str, payload: Any) -> None:
            task = asyncio.create_task(client.send_event(message_id, event_name, payload))
            pending_tasks.add(task)
            task.add_done_callback(pending_tasks.discard)

        def _on_lifecycle(event: Any) -> None:
            job = event.data.get("job")
            if job is None:
                return
            payload = job.to_dict() if hasattr(job, "to_dict") else job
            _forward(event.event_type.value, payload)

        def _on_output(event: Any) -> None:
            _forward("job_output", event.data)

        def _on_progress(event: Any) -> None:
            _forward("job_progress", event.data)

        unsub: list[Any] = [
            self._db.bus.add_listener(EventType.JOB_QUEUED, _on_lifecycle),
            self._db.bus.add_listener(EventType.JOB_STARTED, _on_lifecycle),
            self._db.bus.add_listener(EventType.JOB_COMPLETED, _on_lifecycle),
            self._db.bus.add_listener(EventType.JOB_FAILED, _on_lifecycle),
            self._db.bus.add_listener(EventType.JOB_CANCELLED, _on_lifecycle),
            self._db.bus.add_listener(EventType.JOB_OUTPUT, _on_output),
            self._db.bus.add_listener(EventType.JOB_PROGRESS, _on_progress),
        ]

        try:
            # Park forever — the connection lifecycle (cancellation
            # of this coroutine when the WS closes) is what ends the
            # subscription.
            await asyncio.Event().wait()
        finally:
            for u in unsub:
                u()

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
        """
        job = self._jobs.get(job_id)
        if not job:
            msg = f"Job not found: {job_id}"
            raise ValueError(msg)

        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(UTC).isoformat()
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
        raise ValueError(msg)

    @api_command("firmware/clear")
    async def clear(self, *, status: JobStatus | str | None = None, **kwargs: Any) -> None:
        """
        Remove finished jobs from the list.

        If ``status`` is given, only remove jobs with that status.
        Otherwise removes completed, failed, and cancelled jobs.
        """
        terminal = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
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
        loop = asyncio.get_running_loop()

        def _get_types() -> list[dict]:
            storage = StorageJSON.load(ext_storage_path(configuration))
            if storage is None:
                return []
            platform = (storage.target_platform or "").lower()
            try:
                if platform.upper() in ESP32_VARIANTS:
                    platform_ = "esp32"
                elif platform in ("rtl87xx", "bk72xx", "ln882x", "libretiny"):
                    platform_ = "libretiny"
                else:
                    platform_ = platform
                module = importlib.import_module(f"esphome.components.{platform_}")
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
        try:
            while True:
                job = await self._queue.get()
                if job.status == JobStatus.CANCELLED:
                    continue
                await self._execute_job(job)
        except asyncio.CancelledError:
            pass

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

            config_path = str(self._db.settings.rel_path(job.configuration))
            cache_args = self._build_cache_args(job)
            cmd = self._build_command(job.job_type, config_path, job.port, cache_args)
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
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self._current_process = proc

            has_error_in_output = False
            assert proc.stdout is not None  # type narrowing

            def _check_error(text: str) -> None:
                nonlocal has_error_in_output
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

            # Stream stdout in chunks delimited by `\n` _or_ `\r` so
            # carriage-return-based in-place updates (esptool's
            # `Writing at 0x... (5%)\r`, PlatformIO's progress bars)
            # survive the pipe instead of getting buffered until the
            # next newline. Each emitted chunk keeps its trailing
            # terminator so the frontend can decide whether to append
            # a new line or overwrite the last one.
            buf = b""
            while True:
                data = await proc.stdout.read(4096)
                if not data:
                    # EOF — flush any trailing bytes that didn't end
                    # with a terminator (rare but possible).
                    if buf:
                        line = buf.decode("utf-8", errors="replace")
                        job.output.append(line)
                        self._db.bus.fire(
                            EventType.JOB_OUTPUT,
                            {"job_id": job.job_id, "line": line},
                        )
                        _check_error(line)
                        _check_progress(line)
                        buf = b""
                    break
                buf += data
                while buf:
                    nl = buf.find(b"\n")
                    cr = buf.find(b"\r")
                    if nl == -1 and cr == -1:
                        break  # need more bytes before we can split
                    if nl == -1:
                        idx = cr
                    elif cr == -1:
                        idx = nl
                    else:
                        idx = min(nl, cr)
                    chunk = buf[: idx + 1]
                    buf = buf[idx + 1 :]
                    line = chunk.decode("utf-8", errors="replace")
                    job.output.append(line)
                    self._db.bus.fire(
                        EventType.JOB_OUTPUT,
                        {"job_id": job.job_id, "line": line},
                    )
                    _check_error(line)
                    _check_progress(line)

            exit_code = await proc.wait()
            job.exit_code = exit_code
            job.completed_at = datetime.now(UTC).isoformat()

            # If the user cancelled this job mid-run, the subprocess
            # exits non-zero (terminated by signal). Honour that
            # intent rather than reporting it as a generic failure.
            if job.job_id in self._cancel_requested:
                self._cancel_requested.discard(job.job_id)
                job.status = JobStatus.CANCELLED
                self._db.bus.fire(EventType.JOB_CANCELLED, {"job": job})
                _LOGGER.info("Job %s cancelled mid-run (exit %s)", job.job_id, exit_code)
            else:
                success = exit_code == 0 and not has_error_in_output
                job.status = JobStatus.COMPLETED if success else JobStatus.FAILED
                if has_error_in_output and exit_code == 0:
                    full_output = "".join(job.output)
                    if "No module named esphome" in full_output:
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
            job.status = JobStatus.CANCELLED
            job.completed_at = datetime.now(UTC).isoformat()
            self._cancel_requested.discard(job.job_id)
            self._db.bus.fire(EventType.JOB_CANCELLED, {"job": job})
            _LOGGER.info("Job %s cancelled (runner shutdown)", job.job_id)
            raise
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            job.completed_at = datetime.now(UTC).isoformat()
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
        """Send SIGTERM to the running subprocess; escalate if it lingers.

        The runner loop is the one that actually finalises the
        ``FirmwareJob`` on exit (so we don't double-write status from
        two coroutines). We only nudge the process here.
        """
        proc = self._current_process
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
        except TimeoutError:
            _LOGGER.warning(
                "Subprocess for job %s ignored SIGTERM after %.1fs — sending SIGKILL",
                self._current_job.job_id if self._current_job else "?",
                _TERMINATE_GRACE_SECONDS,
            )
            with suppress(ProcessLookupError):
                proc.kill()

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

        if not esphome_root.exists():
            _emit("Nothing to do — .esphome/ does not exist yet.")
        else:
            for name in _RESET_BUILD_ENV_TARGETS:
                # rmtree isn't interruptible from another coroutine,
                # so we can only stop before starting the next target.
                if job.job_id in self._cancel_requested:
                    self._cancel_requested.discard(job.job_id)
                    _emit("Reset cancelled by user.")
                    job.status = JobStatus.CANCELLED
                    job.completed_at = datetime.now(UTC).isoformat()
                    self._db.bus.fire(EventType.JOB_CANCELLED, {"job": job})
                    return

                target = esphome_root / name
                if not target.exists():
                    _emit(f"  skipped (not present): {name}/")
                    continue
                _emit(f"  removing {name}/ ...")
                await loop.run_in_executor(None, shutil.rmtree, target)
                _emit(f"  removed {name}/")

        _emit(
            "Reset complete — the next compile will re-download "
            "toolchains and re-fetch external components."
        )
        job.exit_code = 0
        job.completed_at = datetime.now(UTC).isoformat()
        job.status = JobStatus.COMPLETED
        job.progress = 100
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

        proc = await asyncio.create_subprocess_exec(
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
    ) -> list[str]:
        """Build the esphome CLI command for a given job type."""
        cmd_map = {
            JobType.COMPILE: "compile",
            JobType.UPLOAD: "upload",
            JobType.INSTALL: "run",
            JobType.CLEAN: "clean",
        }
        # cache_args go before the subcommand — esphome's argparse parses
        # them on the top-level parser, not the per-subcommand one.
        cmd = [*self._esphome_cmd, *(cache_args or []), cmd_map[job_type], config_path]
        if job_type == JobType.INSTALL:
            # Without --no-logs the CLI tails logs forever after the
            # upload, never returning — the job would never complete.
            cmd.append("--no-logs")
        if job_type in (JobType.UPLOAD, JobType.INSTALL) and port:
            cmd.extend(["--device", port])
        return cmd

    def _build_cache_args(self, job: FirmwareJob) -> list[str]:
        """Return ``--mdns/--dns-address-cache`` args for *job*, or empty."""
        # Only OTA uploads benefit — serial flashes don't talk to the
        # device's network address at all.
        if job.job_type not in (JobType.UPLOAD, JobType.INSTALL):
            return []
        if job.port != "OTA":
            return []
        if self._db.devices is None:
            return []
        return self._db.devices.get_address_cache_args(job.configuration)

    # ------------------------------------------------------------------
    # Internals — job management
    # ------------------------------------------------------------------

    def _create_job(self, configuration: str, job_type: JobType, port: str = "") -> FirmwareJob:
        """Create a new job and add it to the in-memory map."""
        job = FirmwareJob(
            job_id=uuid4().hex[:12],
            configuration=configuration,
            job_type=job_type,
            created_at=datetime.now(UTC).isoformat(),
            port=port,
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
        """
        await self._queue.put(job)
        self._db.bus.fire(EventType.JOB_QUEUED, {"job": job})
        if job.configuration:
            await self._supersede_active_jobs(job.configuration, exclude_job_id=job.job_id)
        await self._persist_jobs()
        return job

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
        terminal_states = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}

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
        primary.sort(key=lambda j: j.created_at, reverse=True)
        seen_configs: set[str] = set()
        deduped_primary: list[FirmwareJob] = []
        for job in primary:
            if job.configuration:
                if job.configuration in seen_configs:
                    continue
                seen_configs.add(job.configuration)
            deduped_primary.append(job)
        deduped_primary = deduped_primary[:_MAX_PRIMARY_TERMINAL_JOBS]

        aux.sort(key=lambda j: j.created_at, reverse=True)
        aux = aux[:_MAX_AUX_TERMINAL_JOBS]

        self._jobs = {j.job_id: j for j in (*active, *deduped_primary, *aux)}

    # ------------------------------------------------------------------
    # Internals — persistence
    # ------------------------------------------------------------------

    async def _load_jobs(self) -> None:
        """Load persisted jobs and re-queue any incomplete ones."""
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _load_metadata, self._db.settings.config_dir)
        for job_data in data.get(_JOBS_KEY, []):
            try:
                job = FirmwareJob.from_dict(job_data)
                self._jobs[job.job_id] = job
                if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                    job.status = JobStatus.QUEUED
                    await self._queue.put(job)
            except Exception:
                _LOGGER.warning("Failed to restore job: %s", job_data.get("job_id", "?"))

    async def _persist_jobs(self) -> None:
        """Save all jobs to disk."""
        loop = asyncio.get_running_loop()
        config_dir = self._db.settings.config_dir

        def _save() -> None:
            with metadata_transaction(config_dir) as data:
                data[_JOBS_KEY] = [j.to_dict() for j in self._jobs.values()]

        await loop.run_in_executor(None, _save)
