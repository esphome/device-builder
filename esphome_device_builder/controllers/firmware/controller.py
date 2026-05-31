"""
Firmware build queue + WS command surface.

Owns the persistent single-job queue and the lifecycle event
broadcasts; the bulk of each concern lives in sibling submodules
(``runner`` / ``factories`` / ``jobs`` / ``follow`` / ``clean`` /
``download`` / ``bulk`` / ``cli`` / ``persistence`` / ``lifecycle``).
Public API is the ``@api_command``-decorated methods; everything
else is private.
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
    QueueStatus,
)
from . import bulk, cli, factories, follow, jobs, lifecycle, persistence, runner
from . import clean as clean_mod
from . import download as download_mod
from ._state import FirmwareState
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
        self.state = FirmwareState()
        # Short-lived capability tokens for the HTTP artifact-download route.
        self.download_tokens = download_mod.DownloadTokens()
        self._runner_task: asyncio.Task | None = None
        # Serializes ``persist_jobs`` so a slow executor write can't be
        # overtaken by a newer one (which would let a stale snapshot
        # overwrite fresher state on disk).
        self._persist_lock = asyncio.Lock()

    @property
    def bus(self) -> EventBus:
        """The event bus for lifecycle / output events — read-only shorthand for ``_db.bus``."""
        return self._db.bus

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def queue_status_snapshot(self) -> QueueStatus:
        """Return a :class:`QueueStatus` snapshot of the firmware queue; sync, no I/O.

        ``idle`` and ``running`` aren't redundant with each other:
        ``running=False, queue_depth>0`` is the window between
        ``_queue.put`` and the runner's ``_queue.get``, so a
        scheduler reading only ``running`` would misclassify a
        fully-loaded receiver as accepting more work.
        """
        running = self.state.current_job is not None
        queue_depth = self.state.queue.qsize()
        idle = not running and queue_depth == 0
        return QueueStatus(idle=idle, running=running, queue_depth=queue_depth)

    async def start(self) -> None:
        """Start the queue processor and restore persisted jobs."""
        self.state.esphome_cmd = _find_esphome_cmd()
        _LOGGER.info(
            "ESPHome command: %s (interpreter: %s)",
            " ".join(self.state.esphome_cmd),
            sys.executable,
        )
        ok, detail = await _verify_esphome_importable(self.state.esphome_cmd)
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
        """Queue a compile job; paired-receiver auto-routing unless *force_local*.

        A paired-connected receiver makes the job ``source=REMOTE``
        and the artifacts stage back locally for the frontend's
        "Download firmware binary" button.
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
        """Queue an upload job; ``port`` is forwarded as ``--device`` to esphome.

        ``port`` accepts ``"OTA"`` (CLI resolves the YAML's
        ``esphome.address``), a serial path (``/dev/ttyUSB0``,
        ``COM3``), or an explicit IPv4 / IPv6 / ``.local``
        hostname (bypasses the address cache).
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
        Queue a full reset of the build environment via ``esphome clean-all``.

        Wipes ``<config_dir>/.esphome/`` (except ``storage/``) plus
        PlatformIO's ``core_dir`` / ``cache_dir`` / ``packages_dir``
        / ``platforms_dir`` — for venv users that's the whole
        ``~/.platformio/`` tree; the addon / docker images contain
        the blast radius inside the data dir. The next compile
        re-fetches everything from scratch (slow but thorough).
        Runs through the single-job queue so it can't race a build.
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
        """Queue a device update (compile + upload); paired-receiver auto-routing.

        ``port`` defaults to ``"OTA"`` and accepts the same values
        as :meth:`upload`. When a paired receiver is APPROVED +
        peer-link-connected, the scheduler picks REMOTE — the
        compile dispatches to the receiver and artifacts stage back
        locally for the flash step. ``force_local=True`` overrides
        the scheduler (used by the install dialog's "Build locally
        instead" link).
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
        """Queue a rename: compile + OTA-install the new firmware, then swap the YAML.

        Routed through the single-job queue so it can't race a
        compile / install and appears in the firmware-tasks list
        with live output. ``esphome rename`` keeps the old YAML
        around until the install succeeds — a failed install rolls
        back the new-YAML write so the user can retry against the
        unchanged old hostname.
        """
        await self._validate_configuration_boundary(configuration)
        # Validate the derived ``<new_name>.yaml`` filename at the WS
        # boundary so a direct request can't pass a traversal-shaped
        # name and surface as a failed job later.
        new_filename = f"{new_name}.yaml"
        await self._validate_configuration_boundary(new_filename)
        # Same-name rename is a YAML no-op but still queues a real
        # compile + flash — make the caller use ``firmware/install``.
        if new_filename == configuration:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                "new_name must differ from the current device name",
            )
        # Reject up-front if the target filename is in use. A direct
        # WS client can bypass ``DevicesController.rename_device``'s
        # check, and ``esphome rename`` doesn't check collisions
        # itself — it would blindly overwrite the other device's YAML
        # and flash the wrong firmware. ``new_filename`` already
        # passed ``rel_path`` so build the path directly.
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

    def find_remote_peer_job(self, *, remote_peer: str, remote_job_id: str) -> FirmwareJob | None:
        """Return the FirmwareJob matching (*remote_peer*, *remote_job_id*), or None."""
        return jobs.find_remote_peer_job(self, remote_peer=remote_peer, remote_job_id=remote_job_id)

    def remote_peer_job_ids(self, *, remote_peer: str) -> list[str]:
        """Return the ``remote_job_id`` of every job submitted by *remote_peer*."""
        return jobs.remote_peer_job_ids(self, remote_peer=remote_peer)

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

    # Artifact bytes are served over HTTP (GET /api/firmware/download), not the
    # WebSocket — a ~14 MB firmware.elf exceeds a proxy's WS max_msg_size, and a
    # navigation streams to disk (mobile-friendly). This command mints the
    # single-use token that authorizes one such download.
    @api_command("firmware/download_token")
    async def download_token(self, *, configuration: str, file: str, **kwargs: Any) -> dict:
        await self._validate_configuration_boundary(configuration)
        # Resolve up front so the caller learns the exact filename the download
        # will save under (so the UI's "saved as …" matches the file), and so a
        # missing artifact fails here rather than on the download navigation.
        loop = asyncio.get_running_loop()
        try:
            _, filename = await loop.run_in_executor(
                None, download_mod._resolve_artifact_path, configuration, file
            )
        except (FileNotFoundError, ValueError) as err:
            raise CommandError(ErrorCode.NOT_FOUND, "Firmware artifact not found") from err
        return {"token": self.download_tokens.create(configuration, file), "filename": filename}

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
            self.state.esphome_cmd, job_type, config_path, port, cache_args, new_name
        )

    def _build_cache_args(self, job: FirmwareJob) -> list[str]:
        return cli.build_cache_args(self, job)

    # ------------------------------------------------------------------
    # Internals — job management
    # ------------------------------------------------------------------

    def _sync_validate_configuration_boundary(self, configuration: str) -> None:
        """
        Sync ``rel_path`` traversal check; raise ``CommandError`` on bad input.

        Empty strings raise too — only ``reset_build_env`` wants the
        empty value, and it bypasses this validator entirely. Must
        NOT be called from the event loop directly; ``rel_path``
        calls blocking ``Path.resolve``.
        """
        if not configuration:
            raise CommandError(ErrorCode.INVALID_ARGS, "configuration must not be empty")
        self._db.settings.rel_path(configuration)

    async def _validate_configuration_boundary(self, configuration: str) -> None:
        """Validate one ``configuration`` inside an executor."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._sync_validate_configuration_boundary, configuration)

    async def _validate_configurations_boundary(self, configurations: list[str]) -> None:
        """
        Validate every config in one executor task; raise ``INVALID_ARGS`` on any bad entry.

        One executor task for the whole batch — per-config dispatch
        adds context-switch overhead that scales badly. The whole
        batch fails on a single bad entry rather than silently
        dropping it; transient state conflicts (rename-lock) are
        handled separately by the bulk handlers' skip-and-continue.
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
