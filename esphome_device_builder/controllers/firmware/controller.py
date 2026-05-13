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
import logging
import sys
from collections.abc import Iterator
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError, api_command
from ...models import (
    LOCAL_JOB_BUILD_SOURCE,
    ErrorCode,
    FirmwareJob,
    JobBuildSource,
    JobStatus,
    JobType,
)
from . import bulk, cli, factories, follow, jobs, lifecycle, persistence, runner
from . import clean as clean_mod
from . import download as download_mod
from .helpers import (
    _find_esphome_cmd,
    _validate_port,
    _verify_esphome_importable,
)

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)


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
        return await bulk.compile_bulk(self, configurations=configurations, force_local=force_local)

    @api_command("firmware/install_bulk")
    async def install_bulk(
        self, *, configurations: list[str], port: str = "OTA", **kwargs: Any
    ) -> list[FirmwareJob]:
        return await bulk.install_bulk(self, configurations=configurations, port=port)

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
        return await jobs.get_jobs(self, status=status, configuration=configuration)

    @api_command("firmware/get_job")
    async def get_job(self, *, job_id: str, **kwargs: Any) -> FirmwareJob | None:
        return await jobs.get_job(self, job_id=job_id)

    def active_remote_peer_jobs(self) -> Iterator[FirmwareJob]:
        return jobs.active_remote_peer_jobs(self)

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
        await jobs.cancel(self, job_id=job_id)

    @api_command("firmware/clear")
    async def clear(self, *, status: JobStatus | str | None = None, **kwargs: Any) -> None:
        await jobs.clear(self, status=status)

    # ------------------------------------------------------------------
    # API commands — binary download
    # ------------------------------------------------------------------

    @api_command("firmware/get_binaries")
    async def get_binaries(self, *, configuration: str, **kwargs: Any) -> list[dict]:
        return await download_mod.get_binaries(self, configuration=configuration)

    @api_command("firmware/download")
    async def download(
        self,
        *,
        configuration: str,
        file: str,
        compressed: bool = False,
        **kwargs: Any,
    ) -> dict:
        return await download_mod.download(
            self, configuration=configuration, file=file, compressed=compressed
        )

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
