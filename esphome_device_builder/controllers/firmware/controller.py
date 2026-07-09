"""
Firmware build queue + WS command surface.

Owns the persistent two-lane queue (a compile lane + an upload lane that
run concurrently) and the lifecycle event broadcasts; the bulk of each
concern lives in sibling submodules
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

from esphome.const import __version__ as _installed_esphome_version

from ...controllers.remote_build.env_provisioner import EnvProvisionError
from ...helpers.api import CommandError, api_command
from ...helpers.async_ import create_eager_task, drain_tasks, run_in_executor
from ...helpers.device_yaml import configuration_filename
from ...helpers.event_bus import Event
from ...models import (
    COMPILING_JOB_TYPES,
    LOCAL_JOB_BUILD_SOURCE,
    OTA_PORT,
    DeviceState,
    ErrorCode,
    EventType,
    FirmwareJob,
    JobBuildSource,
    JobStatus,
    JobType,
    QueueStatus,
)
from . import (
    bulk,
    cli,
    factories,
    follow,
    jobs,
    lifecycle,
    persistence,
    remote_dispatch,
    rename_flow,
    runner,
)
from . import clean as clean_mod
from . import download as download_mod
from ._state import FirmwareState, Lane
from .helpers import (
    _find_esphome_cmd,
    _ingest_output_line,
    _validate_upload_target,
    _verify_esphome_importable,
)

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.event_bus import EventBus

_LOGGER = logging.getLogger(__name__)


class FirmwareController:  # noqa: PLR0904 (grandfathered; new public methods need a refactor first)
    """
    Manage firmware build jobs with a persistent two-lane queue.

    A compile lane (CPU) and an upload lane (network) each run one job at a
    time but run concurrently, so a slow upload doesn't block the next
    compile. Jobs are persisted to disk so they survive page refreshes and
    server restarts. Progress is broadcast via the event bus to all
    connected clients.
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

        self._unsub_device_wake = self.bus.add_listener(
            EventType.DEVICE_STATE_CHANGED, self._handle_device_wake
        )
        # Rides the bus (not the lane runner) so pool-dispatched remote
        # compiles arm the queued update too.
        self._unsub_job_completed = self.bus.add_listener(
            EventType.JOB_COMPLETED, self._handle_job_completed
        )

    def stop(self) -> None:
        """Tear down bus subscriptions registered in __init__."""
        self._unsub_device_wake()
        self._unsub_job_completed()

    @property
    def bus(self) -> EventBus:
        """The event bus for lifecycle / output events — read-only shorthand for ``_db.bus``."""
        return self._db.bus

    def _disarm_all_queued_updates(self) -> None:
        """Clear every armed device — a global wipe leaves nothing to flash.

        Without this, a still-armed device would OTA-fail on every wake.
        """
        if self._db.devices is None:
            return
        for device in self._db.devices.get_devices():
            if device.queued_update:
                self._db.devices.clear_queued_update(device.configuration)

    def _dispatch_queued_upload(self, configuration: str) -> None:
        """Queue the deferred OTA flash for *configuration*.

        The job is created synchronously so a flapping device's second
        wake sees it in ``active_jobs()`` before the async enqueue runs —
        the window that let a flap dispatch a superseding second OTA.
        *configuration* comes from our own device events, so the WS
        boundary/port validation ``upload()`` performs is tautological.
        """
        job = self._create_job(configuration, JobType.UPLOAD, port=OTA_PORT)
        self._db.create_background_task(self._enqueue(job))

    def _device_for_configuration(self, configuration: str) -> Any | None:
        """Resolve a Device by its configuration filename."""
        if self._db.devices is None:
            return None

        return self._db.devices.get_by_configuration(configuration)

    def _handle_device_wake(self, event: Event) -> None:
        """Trigger a device's queued update when it comes online.

        ``Device.queued_update`` is the single arm state — no
        controller-side set to go stale when a rename moves the
        configuration filename. The active-flash check is the flap
        guard: a wake bouncing mid-flash must not supersede the
        upload that's already running.
        """
        if event.data["state"] != DeviceState.ONLINE.value:
            return

        config = event.data["configuration"]
        device = self._device_for_configuration(config)
        if not device or not device.queued_update:
            return
        if any(
            job.is_network_flash and job.configuration == config for job in self.state.active_jobs()
        ):
            return

        _LOGGER.info("Device %s woke up. Triggering queued offline update.", config)
        self._dispatch_queued_upload(config)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def lane_status(self, lane: Lane) -> QueueStatus:
        """Return a :class:`QueueStatus` snapshot of one lane; sync, no I/O.

        ``idle`` and ``running`` aren't redundant: ``running=False,
        queue_depth>0`` is the window between ``queue.put`` and the
        runner's ``queue.get``, so a scheduler reading only ``running``
        would misclassify a loaded lane as accepting more work.
        """
        running = lane.current_job is not None
        queue_depth = lane.queue.qsize()
        idle = not running and queue_depth == 0
        return QueueStatus(idle=idle, running=running, queue_depth=queue_depth)

    def compile_queue_status(self) -> QueueStatus:
        """Compile-lane status — what a remote offloader keys on (a receiver only compiles).

        An uploading receiver still has a free compile lane, so it must keep
        advertising idle for delegated compiles rather than the aggregate.
        """
        return self.lane_status(self.state.compile_lane)

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
        return await factories.enqueue_compile(
            self, configuration=configuration, force_local=force_local
        )

    @api_command("firmware/upload")
    async def upload(
        self, *, configuration: str, port: str = "", bootloader: bool = False, **kwargs: Any
    ) -> FirmwareJob:
        """Queue an upload job; ``port`` is forwarded as ``--device`` to esphome.

        ``port`` accepts ``"OTA"`` (CLI resolves the YAML's
        ``esphome.address``), a serial path (``/dev/ttyUSB0``,
        ``COM3``), or an explicit IPv4 / IPv6 / ``.local``
        hostname (bypasses the address cache).
        ``bootloader=True`` flashes the bootloader image instead of
        the app (``esphome upload --bootloader``); OTA targets only.
        """
        _validate_upload_target(port, bootloader=bootloader)
        await self._validate_configuration_boundary(configuration)
        job = self._create_job(
            configuration, JobType.UPLOAD, port=port, flash_bootloader=bootloader
        )
        return await self._enqueue(job)

    @api_command("firmware/clean")
    async def clean(self, *, configuration: str, **kwargs: Any) -> FirmwareJob:
        # Disarm a queued update — the wipe leaves nothing to flash, and a
        # still-armed device would OTA-fail on every wake.
        if self._db.devices is not None:
            self._db.devices.clear_queued_update(configuration)
        return await clean_mod.clean(self, configuration=configuration)

    @api_command("firmware/clear_queued_update")
    async def clear_queued_update(self, *, configuration: str, **kwargs: Any) -> None:
        """Manually clear the queued_update flag for a device."""
        await self._validate_configuration_boundary(configuration)

        devices = self._db.devices
        device = self._device_for_configuration(configuration)
        if devices is None or not device or not device.queued_update:
            return

        devices.clear_queued_update(configuration)
        _LOGGER.info("Queued update cleared for device %s", configuration)

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

        Cancels every other in-flight job first so the wipe can't race a live
        compile or upload running concurrently on either lane.
        """
        self._disarm_all_queued_updates()
        job = self._create_job("", JobType.RESET_BUILD_ENV)
        # The global sweep re-raises on a state-out-of-sync RuntimeError; roll the
        # just-created RESET job back so the orphan can't wedge the upload lane
        # (it counts as an active reset in ``upload_blocked``) or wipe on restart.
        try:
            await factories.cancel_all_active_jobs(self, exclude_job_ids={job.job_id})
        except BaseException:
            self.state.jobs.pop(job.job_id, None)
            raise
        return await self._enqueue(job)

    @api_command("firmware/install")
    async def install(
        self,
        *,
        configuration: str,
        port: str = OTA_PORT,
        force_local: bool = False,
        bootloader: bool = False,
        **kwargs: Any,
    ) -> FirmwareJob:
        """Queue a device update (compile + upload); paired-receiver auto-routing.

        ``port`` defaults to ``"OTA"`` and accepts the same values
        as :meth:`upload`. When a paired receiver is APPROVED +
        peer-link-connected, the scheduler picks REMOTE — the
        compile dispatches to the receiver and artifacts stage back
        locally for the flash step. ``force_local=True`` overrides
        the scheduler (used by the install dialog's "Build locally
        instead" link). ``bootloader=True`` compiles then flashes
        the bootloader image instead of the app (``esphome upload
        --bootloader``); OTA targets only, device must be reachable.
        """
        _validate_upload_target(port, bootloader=bootloader)
        await self._validate_configuration_boundary(configuration)

        # Install is a compile + a dependent local upload. The compile (local
        # or dispatched to a receiver) materialises the binary locally; the
        # upload then flashes on the upload lane, freeing the compile lane to
        # build the next device — so a slow flash never blocks a compile, and
        # a remote receiver keeps compiling while we upload locally.
        return await factories.enqueue_install_or_defer(
            self,
            configuration=configuration,
            port=port,
            force_local=force_local,
            flash_bootloader=bootloader,
        )

    @api_command("firmware/rename")
    async def rename(self, *, configuration: str, new_name: str, **kwargs: Any) -> FirmwareJob:
        """Queue a rename chain (see :meth:`rename_chain`); returns the COMPILE head."""
        head, _tail = await self.rename_chain(configuration=configuration, new_name=new_name)
        return head

    async def rename_chain(
        self,
        *,
        configuration: str,
        new_name: str,
        content: str | None = None,
        new_content: str | None = None,
    ) -> tuple[FirmwareJob, FirmwareJob]:
        """Queue a rename as a COMPILE of the renamed YAML + a flash-and-swap tail.

        The renamed YAML is written up-front; the compile is remote-eligible
        like an install's, and the tail OTA-flashes the *old* device address
        then drops the old YAML. A failed / cancelled chain deletes the new
        YAML so the old device is untouched.
        """
        await self._validate_configuration_boundary(configuration)
        # Validate the derived ``<new_name>.yaml`` filename at the WS
        # boundary so a direct request can't pass a traversal-shaped
        # name and surface as a failed job later.
        new_filename = configuration_filename(new_name)
        await self._validate_configuration_boundary(new_filename)
        # Same-name rename is a YAML no-op but still queues a real
        # compile + flash — make the caller use ``firmware/install``.
        if new_filename == configuration:
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                "new_name must differ from the current device name",
            )
        # Reject up-front if the target filename is in use — a chain
        # would blindly overwrite the other device's YAML and flash the
        # wrong firmware. A retry is exempt: the on-disk file belongs to
        # the still-active chain the retry supersedes. ``new_filename``
        # already passed ``rel_path`` so build the path directly.
        new_path = self._db.settings.config_dir / new_filename
        if await run_in_executor(new_path.exists) and not rename_flow.active_chain_owns_target(
            self, configuration, new_name
        ):
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"A device named {new_filename} already exists",
            )
        return await rename_flow.begin_rename(
            self,
            configuration=configuration,
            new_name=new_name,
            content=content,
            new_content=new_content,
        )

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
        self, *, configurations: list[str], port: str = OTA_PORT, **kwargs: Any
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
        try:
            _, filename = await run_in_executor(
                download_mod._resolve_artifact_path, configuration, file
            )
        except (FileNotFoundError, ValueError) as err:
            raise CommandError(ErrorCode.NOT_FOUND, "Firmware artifact not found") from err
        return {"token": self.download_tokens.create(configuration, file), "filename": filename}

    # ------------------------------------------------------------------
    # Internals — queue processing
    # ------------------------------------------------------------------

    async def _run_queue(self) -> None:
        """Run both lane consumers + the remote-dispatch loop; drain all on any exit.

        On shutdown-cancel or one task raising, cancel and await all three
        before the error propagates — else a sibling is left orphaned
        mid-flight (subprocess not terminated, job not finalised). The
        remote-dispatch loop never returns on its own; cancelling it
        detaches its bus listeners.
        """
        queue_tasks = [
            create_eager_task(runner.run_lane(self, self.state.compile_lane)),
            create_eager_task(runner.run_lane(self, self.state.upload_lane)),
            create_eager_task(remote_dispatch.run_dispatch_loop(self)),
        ]
        try:
            await asyncio.gather(*queue_tasks)
        finally:
            await drain_tasks(queue_tasks)

    async def _execute_job(self, job: FirmwareJob, lane: Lane) -> None:
        await runner.execute_job(self, job, lane)

    def _handle_job_completed(self, event: Event) -> None:
        job = event.data["job"]
        self._handle_deferred_compile_completion(job)
        self._handle_ota_upload_completion(job)

    def _handle_deferred_compile_completion(self, job: FirmwareJob) -> None:
        """After a deferred-install COMPILE finishes, upload now or arm for later.

        Only a successfully-completed COMPILE queued via the offline-install
        path (``is_deferred_install``) does anything here — a plain compile,
        or one that failed, has nothing to act on.
        """
        if not job.is_deferred_compile_success:
            return

        devices = self._db.devices
        if not devices:
            return

        device = self._device_for_configuration(job.configuration)
        if not device:
            return

        # Arm first on every path — a failed immediate OTA must find the flag
        # set so the device stays armed for its next wake. Anything short of
        # confirmed ONLINE (OFFLINE, or the narrow UNKNOWN window from a
        # scanner rebuild with previous=None) just waits for that wake.
        devices.set_queued_update(job.configuration)
        if device.state == DeviceState.ONLINE:
            _LOGGER.info(
                "Device %s is online after deferred compile. Triggering upload now.",
                job.configuration,
            )
            self._dispatch_queued_upload(job.configuration)

    def _handle_ota_upload_completion(self, job: FirmwareJob) -> None:
        """Disarm a queued update once its OTA delivery lands.

        A failed / cancelled attempt never reaches the JOB_COMPLETED
        listener, so the flag stays set and the device stays armed for
        its next wake.
        """
        devices = self._db.devices
        if devices is None or not job.is_completed_ota_upload:
            return

        device = self._device_for_configuration(job.configuration)
        if not device or not device.queued_update:
            return

        devices.clear_queued_update(job.configuration)

    async def _execute_remote_job(self, job: FirmwareJob) -> None:
        await runner.execute_remote_job(self, job)

    def _tracked_subprocess(
        self, lane: Lane, *args: Any, **kwargs: Any
    ) -> AbstractAsyncContextManager[asyncio.subprocess.Process]:
        return runner.tracked_subprocess(self, lane, *args, **kwargs)

    def _finalize_terminal(
        self, job: FirmwareJob, status: JobStatus, *, error: str | None = None
    ) -> None:
        lifecycle.finalize_terminal(self, job, status, error=error)

    def _finalize_cancelled(self, job: FirmwareJob) -> None:
        lifecycle.finalize_cancelled(self, job)

    async def _terminate_current_process(self, lane: Lane) -> None:
        await lifecycle.terminate_current_process(self, lane)

    async def _verify_chip(self, job: FirmwareJob, lane: Lane) -> None:
        await cli.verify_chip(self, job, lane)

    def _compose_subprocess_env(self, job: FirmwareJob) -> dict[str, str]:
        return cli.compose_subprocess_env(job)

    def _build_command(
        self,
        job_type: JobType,
        config_path: str,
        port: str,
        cache_args: list[str] | None = None,
        new_name: str = "",
        *,
        flash_bootloader: bool = False,
        esphome_cmd: list[str] | None = None,
    ) -> list[str]:
        return cli.build_command(
            esphome_cmd or self.state.esphome_cmd,
            job_type,
            config_path,
            port,
            cache_args,
            new_name,
            flash_bootloader=flash_bootloader,
        )

    async def _resolve_esphome_cmd(self, job: FirmwareJob) -> list[str]:
        """
        Return the esphome CLI invocation to run *job* with.

        For a remote job whose ``target_esphome_version`` differs from ours:
        COMPILE / INSTALL provision that version's venv (raising
        :class:`EnvProvisionError` if unavailable); CLEAN reuses it only when
        already provisioned. Everything else uses the installed esphome.
        """
        version = job.target_esphome_version
        if not version or version == _installed_esphome_version:
            return self.state.esphome_cmd
        receiver = self._db.remote_build_receiver
        provisioner = receiver.state.env_provisioner if receiver is not None else None
        if job.job_type in COMPILING_JOB_TYPES:
            if provisioner is None:
                raise EnvProvisionError(
                    f"no provisioner available to build esphome {version} "
                    "(receiver stopping?); refusing to compile with the installed version"
                )
            return await provisioner.provision(version)
        if job.job_type is JobType.CLEAN:
            if provisioner is not None and (cached := await provisioner.cached_cmd(version)):
                return cached
            # No cached venv for the built version: clean with the installed
            # esphome. An older esphome cleans less (managed_components, idedata,
            # pio_components, PIO cache), so surface the possible under-purge
            # instead of letting it pass silently.
            _LOGGER.info(
                "Clean %s: no cached esphome %s venv; cleaning with the installed "
                "esphome, which may not fully purge the build",
                job.configuration,
                version,
            )
            _ingest_output_line(
                job,
                self._db.bus,
                f"No cached esphome {version}; cleaning with the installed esphome, "
                f"which may not fully purge artifacts built with {version}.\n",
            )
        return self.state.esphome_cmd

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
        await run_in_executor(self._sync_validate_configuration_boundary, configuration)

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

        await run_in_executor(_validate_all)

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
        target_esphome_version: str = "",
        *,
        flash_bootloader: bool = False,
    ) -> FirmwareJob:
        return factories.create_job(
            self,
            configuration,
            job_type,
            port=port,
            flash_bootloader=flash_bootloader,
            new_name=new_name,
            remote_peer=remote_peer,
            remote_peer_label=remote_peer_label,
            remote_job_id=remote_job_id,
            build_source=build_source,
            device_name=device_name,
            device_friendly_name=device_friendly_name,
            target_esphome_version=target_esphome_version,
        )

    def _resolve_install_source(self, *, force_local: bool = False) -> JobBuildSource:
        return factories.resolve_install_source(self, force_local=force_local)

    async def _enqueue(self, job: FirmwareJob, *, supersede: bool = True) -> FirmwareJob:
        return await factories.enqueue(self, job, supersede=supersede)

    def _check_rename_lock(self, job: FirmwareJob) -> None:
        factories.check_rename_lock(self, job)

    async def _supersede_active_jobs(
        self, configuration: str, *, exclude_job_ids: set[str]
    ) -> None:
        await factories.supersede_active_jobs(self, configuration, exclude_job_ids=exclude_job_ids)

    def _prune_history(self) -> None:
        persistence.prune_history(self)

    # ------------------------------------------------------------------
    # Internals — persistence
    # ------------------------------------------------------------------

    async def _load_jobs(self) -> None:
        await persistence.load_jobs(self)

    async def _persist_jobs(self) -> None:
        await persistence.persist_jobs(self)
