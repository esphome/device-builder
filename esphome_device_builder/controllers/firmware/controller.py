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
import sys
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
from operator import attrgetter
from typing import TYPE_CHECKING, Any

from esphome.components.esp32 import VARIANTS as ESP32_VARIANTS
from esphome.components.libretiny.const import (
    FAMILY_COMPONENT as _LIBRETINY_FAMILY_COMPONENT,
)
from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError, api_command
from ...helpers.storage_path import resolve_storage_path
from ...models import (
    LOCAL_JOB_BUILD_SOURCE,
    TERMINAL_JOB_STATUSES,
    ErrorCode,
    EventType,
    FirmwareJob,
    JobBuildSource,
    JobLifecycleData,
    JobStatus,
    JobType,
)
from . import clean as clean_mod
from . import cli, factories, follow, lifecycle, persistence, runner
from .constants import (
    _ACTIVE_JOB_STATUSES,
)
from .helpers import (
    _find_esphome_cmd,
    _mark_job_terminal,
    _validate_port,
    _verify_esphome_importable,
)

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.event_bus import EventBus

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


class FirmwareController:  # noqa: PLR0904 (grandfathered; new public methods need a refactor first)
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
        # Per-job ``asyncio.Event`` that the cancel handler signals
        # so an in-flight runner can wake instantly instead of
        # polling. Only the remote-source runner registers an event
        # today (the local subprocess path's cancel landing is
        # driven by SIGTERM on the spawned process). The remote
        # runner adds an entry before parking on the terminal wait
        # and clears it on exit.
        self._cancel_events: dict[str, asyncio.Event] = {}

    @property
    def bus(self) -> EventBus:
        """The event bus this controller fires lifecycle / output events on.

        Shorthand for ``self._db.bus`` so collaborators
        (notably ``remote_runner``) don't reach across two
        underscore-prefixed attributes to get at the canonical
        offloader-side bus. Read-only — the bus reference is
        installed by :class:`DeviceBuilder` at construction
        and doesn't move.
        """
        return self._db.bus

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def queue_status_snapshot(self) -> tuple[bool, bool, int]:
        """Return ``(idle, running, queue_depth)`` for the firmware queue.

        Pure synchronous read of the controller's RAM state — no
        executor hop, no disk read. Used by the remote-build
        controller's :meth:`_broadcast_queue_status` to compose
        the receiver-side snapshot for paired offloaders on every
        ``JOB_QUEUED`` / ``JOB_STARTED`` / terminal event tick.

        ``running`` is ``True`` while a single job occupies the
        single-job runner slot (``_current_job is not None``);
        ``queue_depth`` is the count of pending jobs waiting
        their turn (``_queue.qsize()``); ``idle`` is the
        nothing-running-and-nothing-queued state. The three
        fields aren't strictly redundant — the
        ``running=False, queue_depth>0`` window exists between
        ``await _queue.put(job)`` and the runner's ``_queue.get()``
        landing the same item, so a scheduler that reads only
        ``running`` would misclassify a fully-loaded receiver
        as accepting more work.
        """
        running = self._current_job is not None
        queue_depth = self._queue.qsize()
        idle = not running and queue_depth == 0
        return idle, running, queue_depth

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
    async def compile(
        self,
        *,
        configuration: str,
        force_local: bool = False,
        **kwargs: Any,
    ) -> FirmwareJob:
        """Queue a compile job.

        Routes through :func:`helpers.build_scheduler.pick_build_path`
        same as :meth:`install`: a paired-connected receiver makes
        the resulting job ``source=REMOTE`` so the remote runner
        dispatches the compile to the build server and the
        offloader-side materialiser stages the artifacts back
        locally. The frontend's "Download firmware binary" button
        reads from the staged tree via ``firmware/download``.

        ``force_local`` opts out so a user can build locally
        despite an available paired receiver.
        """
        await self._validate_configuration_boundary(configuration)
        build_source = self._resolve_install_source(force_local=force_local)
        job = self._create_job(
            configuration,
            JobType.COMPILE,
            build_source=build_source,
        )
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
        return await clean_mod.clean(self, configuration=configuration)

    @api_command("firmware/reset_build_env")
    async def reset_build_env(self, **kwargs: Any) -> FirmwareJob:
        """
        Queue a full reset of the build environment.

        Shells out to ``esphome clean-all <config_dir>`` (matching
        the legacy dashboard's ``EsphomeCleanAllHandler``), which:

        * wipes every ``<config_dir>/.esphome/`` subdir except
          ``storage/``, plus every top-level non-``.json`` file, and
        * wipes PlatformIO's own ``cache_dir`` / ``packages_dir`` /
          ``platforms_dir`` / ``core_dir`` resolved from PlatformIO's
          config. ``core_dir`` is the umbrella that contains the
          other three by default, so for venv users this collapses
          to wiping the entire ``~/.platformio/`` tree — toolchains,
          framework packages, and the download cache. The HA add-on
          / docker images keep these inside the data dir so the
          blast radius is contained there.

        The next compile re-fetches external components and
        re-downloads toolchains from scratch — slow to recover from
        but the most thorough way to escape a poisoned cache. Runs
        through the same single-job queue as compile/upload so it
        can't race a build in progress.
        """
        job = self._create_job("", JobType.RESET_BUILD_ENV)
        return await self._enqueue(job)

    @api_command("firmware/install")
    async def install(
        self,
        *,
        configuration: str,
        port: str = "OTA",
        force_local: bool = False,
        **kwargs: Any,
    ) -> FirmwareJob:
        """Queue a device update (compile + upload).

        ``port`` defaults to ``"OTA"`` — the CLI resolves the
        configured device's address from the YAML's
        ``esphome.address``. Accepts the same values as
        :meth:`upload`: a serial path for wired flashing, or an
        explicit IP / hostname for "install to a specific address"
        — the address cache is bypassed when the user names the
        target directly.

        Routes through :func:`helpers.build_scheduler.pick_build_path`
        before queuing: when a paired receiver is APPROVED +
        peer-link-connected, the resulting job carries
        ``source=REMOTE`` + ``source_pin_sha256=<pin>`` +
        ``source_label=<receiver_label>`` so the source-routed
        runner dispatches the compile to that receiver and stages
        the resulting artifacts back for the local flash step.
        Otherwise the job stays ``source=LOCAL`` and runs through
        the existing in-process subprocess
        pipeline. Silent fallback by design — the user doesn't
        choose a build location; the scheduler routes
        transparently.

        ``force_local`` opts out of the scheduler decision: the
        install runs LOCAL regardless of what
        :func:`pick_build_path` would have picked. Used by the
        install dialog's "Build locally instead" override link
        next to the "Building on {receiver}" sub-line — the
        operator sees the scheduler picked REMOTE, decides
        they want LOCAL anyway (cache hot locally, paired
        receiver slow this week, network flakey, …), cancels
        the in-flight remote and resubmits with this flag.
        Default ``False`` preserves the transparent-install
        behaviour for every existing caller.
        """
        _validate_port(port)
        await self._validate_configuration_boundary(configuration)
        build_source = self._resolve_install_source(force_local=force_local)
        job = self._create_job(
            configuration,
            JobType.INSTALL,
            port=port,
            build_source=build_source,
        )
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
    async def compile_bulk(
        self,
        *,
        configurations: list[str],
        force_local: bool = False,
        **kwargs: Any,
    ) -> list[FirmwareJob]:
        """Queue compile for multiple devices.

        Per-device errors (most commonly the rename lock) skip that
        device and keep going. Each job routes through
        :meth:`_resolve_install_source` so paired-build auto-routing
        applies (mirrors :meth:`compile` / :meth:`install_bulk`);
        ``force_local=True`` keeps every job LOCAL.
        """
        await self._validate_configurations_boundary(configurations)
        jobs: list[FirmwareJob] = []
        for config in configurations:
            try:
                build_source = self._resolve_install_source(force_local=force_local)
                job = self._create_job(
                    config,
                    JobType.COMPILE,
                    build_source=build_source,
                )
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
                build_source = self._resolve_install_source()
                job = self._create_job(
                    config,
                    JobType.INSTALL,
                    port=port,
                    build_source=build_source,
                )
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

    def active_remote_peer_jobs(self) -> Iterator[FirmwareJob]:
        """Yield every QUEUED / RUNNING job that arrived via the peer-link.

        Synchronous, no-copy generator over :attr:`_jobs` for the
        peer-link tier's lookups (the 6c cleanup sweep keys off
        this to skip in-flight subtrees; future schedulers /
        diagnostics surfaces should call this rather than
        reaching into ``_jobs`` directly). The single-underscore
        prefix on ``_jobs`` marks it as private to the firmware
        controller; this public accessor is the load-bearing
        seam so a future refactor (lock-wrapped jobs map,
        QUEUED + RUNNING split into two dicts, indexed view)
        doesn't silently break callers.

        ``remote_peer`` filters to peer-link-originated jobs
        only — :class:`FirmwareJob.remote_peer` is empty for
        locally-submitted jobs (see :mod:`models.firmware`).
        """
        for job in self._jobs.values():
            if job.status not in _ACTIVE_JOB_STATUSES:
                continue
            if not job.remote_peer:
                continue
            yield job

    @api_command("firmware/follow_job")
    async def follow_job(
        self, *, job_id: str, client: Any = None, message_id: str = "", **kwargs: Any
    ) -> None:
        await follow.follow_job(self, job_id=job_id, client=client, message_id=message_id)

    @api_command("firmware/follow_jobs")
    async def follow_jobs(
        self,
        *,
        client: Any = None,
        message_id: str = "",
        snapshot: bool = True,
        **kwargs: Any,
    ) -> None:
        await follow.follow_jobs(self, client=client, message_id=message_id, snapshot=snapshot)

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
            # Mark + persist before fire so a restart-after-cancel
            # reload sees the job as CANCELLED (the test pins
            # this in ``test_cancelled_job_survives_restart_without_
            # being_requeued``). Doesn't go through
            # :meth:`_finalize_terminal` because the helper
            # collapses mark + fire and we need to land
            # ``_persist_jobs`` in between; the slot-release the
            # helper does is a no-op anyway for a QUEUED job
            # (``_current_job`` belongs to whatever's actually
            # running, not this queue entry).
            _mark_job_terminal(job, JobStatus.CANCELLED)
            self._prune_history()
            await self._persist_jobs()
            cancelled_payload: JobLifecycleData = {"job": job}
            self._db.bus.fire(EventType.JOB_CANCELLED, cancelled_payload)
            return

        if job.status == JobStatus.RUNNING:
            if self._current_job is None or self._current_job.job_id != job_id:
                msg = "Running job is not the active subprocess (state out of sync)"
                raise RuntimeError(msg)
            self._cancel_requested.add(job_id)
            # Wake any runner parked on its cancel event (the
            # source-routed remote runner registers one; the
            # local subprocess path doesn't need one because
            # SIGTERM is the wake signal).
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event is not None:
                cancel_event.set()
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
        terminal = TERMINAL_JOB_STATUSES
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
        # ``resolve_storage_path`` collapses to
        # ``<data_dir>/storage/<Path(configuration).name>.json`` —
        # the basename collapse defangs separators in the
        # configuration but a traversal-shaped *configuration*
        # would still escape the config dir before reaching the
        # closure (e.g. opening a sidecar at an attacker-controlled
        # path under ``<data_dir>/storage``). The validator below
        # is the gate that keeps any traversal payload out of the
        # inner closure entirely. Do not reorder.
        await self._validate_configuration_boundary(configuration)
        loop = asyncio.get_running_loop()

        def _get_types() -> list[dict]:
            storage = StorageJSON.load(resolve_storage_path(configuration))
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
        # See ``get_binaries`` — ``resolve_storage_path`` collapses
        # to ``<data_dir>/storage/<Path(configuration).name>.json``,
        # but a traversal-shaped *configuration* could still resolve
        # to an attacker-controlled basename inside the storage
        # tree (e.g. by stripping segments down to a sensitive
        # leaf), so we re-validate at the WS boundary.
        # ``_validate_configuration_boundary`` is the only gate;
        # do not reorder. Coverage:
        # ``test_download.py::test_download_validator_runs_before_ext_storage_path``.
        await self._validate_configuration_boundary(configuration)
        loop = asyncio.get_running_loop()

        def _read_binary() -> dict:
            storage = StorageJSON.load(resolve_storage_path(configuration))
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
        await runner.run_queue(self)

    async def _execute_job(self, job: FirmwareJob) -> None:
        await runner.execute_job(self, job)

    async def _execute_remote_job(self, job: FirmwareJob) -> None:
        await runner.execute_remote_job(self, job)

    def _tracked_subprocess(
        self, *args: Any, **kwargs: Any
    ) -> AbstractAsyncContextManager[asyncio.subprocess.Process]:
        return runner.tracked_subprocess(self, *args, **kwargs)

    def _finalize_terminal(self, job: FirmwareJob, status: JobStatus) -> None:
        lifecycle.finalize_terminal(self, job, status)

    def _finalize_cancelled(self, job: FirmwareJob) -> None:
        lifecycle.finalize_cancelled(self, job)

    def _raise_if_cancelled(self, job: FirmwareJob, phase: str) -> None:
        lifecycle.raise_if_cancelled(self, job, phase)

    async def _terminate_current_process(self) -> None:
        await lifecycle.terminate_current_process(self)

    async def _verify_chip(self, job: FirmwareJob) -> None:
        await cli.verify_chip(self, job)

    def _compose_subprocess_env(self, job: FirmwareJob) -> dict[str, str]:
        return cli.compose_subprocess_env(job)

    def _build_command(
        self,
        job_type: JobType,
        config_path: str,
        port: str,
        cache_args: list[str] | None = None,
        new_name: str = "",
    ) -> list[str]:
        return cli.build_command(
            self._esphome_cmd, job_type, config_path, port, cache_args, new_name
        )

    def _build_cache_args(self, job: FirmwareJob) -> list[str]:
        return cli.build_cache_args(self, job)

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
        remote_peer: str = "",
        remote_peer_label: str = "",
        remote_job_id: str = "",
        build_source: JobBuildSource = LOCAL_JOB_BUILD_SOURCE,
        device_name: str = "",
        device_friendly_name: str = "",
    ) -> FirmwareJob:
        return factories.create_job(
            self,
            configuration,
            job_type,
            port=port,
            new_name=new_name,
            remote_peer=remote_peer,
            remote_peer_label=remote_peer_label,
            remote_job_id=remote_job_id,
            build_source=build_source,
            device_name=device_name,
            device_friendly_name=device_friendly_name,
        )

    def _resolve_install_source(self, *, force_local: bool = False) -> JobBuildSource:
        return factories.resolve_install_source(self, force_local=force_local)

    async def _enqueue(self, job: FirmwareJob, *, supersede: bool = True) -> FirmwareJob:
        return await factories.enqueue(self, job, supersede=supersede)

    def _check_rename_lock(self, job: FirmwareJob) -> None:
        factories.check_rename_lock(self, job)

    async def _supersede_active_jobs(self, configuration: str, *, exclude_job_id: str) -> None:
        await factories.supersede_active_jobs(self, configuration, exclude_job_id=exclude_job_id)

    def _prune_history(self) -> None:
        persistence.prune_history(self)

    # ------------------------------------------------------------------
    # Internals — persistence
    # ------------------------------------------------------------------

    async def _load_jobs(self) -> None:
        await persistence.load_jobs(self)

    async def _persist_jobs(self) -> None:
        await persistence.persist_jobs(self)
