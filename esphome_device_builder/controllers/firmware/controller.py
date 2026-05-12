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
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from operator import attrgetter
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from esphome.components.esp32 import VARIANTS as ESP32_VARIANTS
from esphome.components.libretiny.const import (
    FAMILY_COMPONENT as _LIBRETINY_FAMILY_COMPONENT,
)
from esphome.core import CORE
from esphome.storage_json import StorageJSON

from ...helpers.api import CommandError, api_command
from ...helpers.build_scheduler import BuildPath, pick_build_path
from ...helpers.event_bus import StreamControls, stream_events
from ...helpers.process import terminate_subtree_with_grace
from ...helpers.remote_build_layout import parse_from_configuration as parse_remote_build_path
from ...helpers.storage_path import resolve_storage_path
from ...helpers.subprocess import create_subprocess_exec, iter_lines_with_progress
from ...models import (
    TERMINAL_JOB_EVENTS,
    TERMINAL_JOB_STATUSES,
    ErrorCode,
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobSource,
    JobStatus,
    JobType,
    StreamEvent,
)
from ...models.remote_build import PeerStatus
from ..config import _load_metadata, metadata_transaction
from .constants import (
    _ERROR_PATTERNS,
    _JOBS_KEY,
    _MAX_AUX_TERMINAL_JOBS,
    _MAX_PRIMARY_TERMINAL_JOBS,
    _OTA_ADDRESS_CACHE_JOB_TYPES,
    _PRIMARY_JOB_TYPES,
    ESPHOME_SUBPROCESS_ENV,
)
from .helpers import (
    _find_esphome_cmd,
    _ingest_output_line,
    _is_no_module_named_esphome,
    _mark_job_terminal,
    _names_touched_by_job,
    _trim_job_output,
    _validate_port,
    _verify_esphome_importable,
)
from .remote_runner import run_remote_job

if TYPE_CHECKING:
    from ...device_builder import DeviceBuilder
    from ...helpers.event_bus import Event, EventBus

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

# Job types that produce build artifacts a clean would destroy.
# A ``firmware/clean`` request that lands while one of these is
# in-flight for the same configuration is rejected loudly rather
# than supersede-cancelled — see the ``clean`` handler's docstring
# for the rationale.
_BUILD_PRODUCING_JOB_TYPES: frozenset[JobType] = frozenset(
    {JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL, JobType.RENAME}
)

# Statuses a job has *while in flight*. ``_jobs`` retains terminal
# entries for the recent-jobs history, so any "is something
# running for this configuration?" check has to filter for these.
_ACTIVE_JOB_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})

# Maps each terminal :class:`JobStatus` to the lifecycle event the
# runner fires when a job reaches that status. Routes through
# :meth:`FirmwareController._finalize_terminal` so every
# finalisation site stays paired with the right event — keeps the
# six-call-site signature consistent.
_STATUS_TO_TERMINAL_EVENT: dict[JobStatus, EventType] = {
    JobStatus.COMPLETED: EventType.JOB_COMPLETED,
    JobStatus.FAILED: EventType.JOB_FAILED,
    JobStatus.CANCELLED: EventType.JOB_CANCELLED,
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
        if force_local:
            source, pin_sha256, label = JobSource.LOCAL, "", ""
        else:
            source, pin_sha256, label = self._resolve_install_source(configuration)
        job = self._create_job(
            configuration,
            JobType.COMPILE,
            source=source,
            source_pin_sha256=pin_sha256,
            source_label=label,
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
        """
        Queue a build clean job, plus one per connected paired receiver.

        Returns the LOCAL clean job (the one the operator's WS
        command is awaiting). N additional REMOTE clean jobs are
        queued silently for fan-out to every currently-connected
        approved peer; each shows up as its own
        :class:`FirmwareJob` in the firmware-jobs list and drives
        the same lifecycle events as remote installs do, so the
        operator sees per-receiver clean progress in the
        existing UI.

        **Why fan out:** a stale receiver-side build dir is the
        same class of problem a stale local build dir is. The
        operator's "Clean build files" click expects every place
        this device has been built to drop its artifacts, not
        just the local one. Without the fan-out, a remote receiver
        keeps caching the broken state and the next remote
        compile picks up the same poisoned tree.

        **Best-effort:** a peer that disconnects between this
        ``clean`` call and the runner picking up its job lands on
        the existing remote-session-lost FAILED path (the runner's
        ``_dispatch_and_drive`` returns ``CommandError`` from
        ``_lookup_open_peer_link_client``). The local job is
        independent and runs regardless. A peer that isn't
        connected at all just doesn't get a job queued — the next
        time the operator clicks clean while that peer is
        connected, it'll catch up.

        Rejects with ``CommandError(INVALID_ARGS)`` when an active
        compile / upload / install / rename job exists for the same
        configuration. Other firmware commands rely on the
        ``_enqueue`` supersede path to cancel-and-replace the running
        job — that's the right shape for "user wants to retry the
        compile" — but a clean wipes the build artifacts the running
        job is producing, so a quietly-cancelled build that the user
        didn't intend to abandon is the worse failure mode. Make the
        user retry once the build settles instead. Two clean jobs
        for the same configuration still supersede each other (the
        second one is the user's intent regardless). The supersede
        check applies only to the LOCAL job; the fan-out's per-peer
        REMOTE jobs enqueue with ``supersede=False`` so they don't
        cancel siblings or the just-queued local clean. See
        :meth:`_enqueue`'s docstring for the carve-out rationale.

        The WS reply returns only the LOCAL clean — that's what the
        operator's ``firmware/clean`` call awaits. Per-peer REMOTE
        clean jobs surface through the existing
        ``subscribe_events`` firmware-jobs stream the dashboard
        already consumes for in-flight job lists, so the operator
        sees N+1 rows in the firmware-tasks panel without the
        handler needing to thread them through the WS reply
        shape. Don't "fix" this to return a list — the WS contract
        is "the handler returns the job the operator's click
        produced"; the fan-out is incidental.

        Multi-offloader fleets: a clean from offloader A and a
        concurrent compile from offloader B against the same
        receiver are safe by construction. Each offloader gets its
        own ``ESPHOME_DATA_DIR`` subtree
        (``<receiver_data_dir>/.remote_builds/<dashboard_id>/.esphome``),
        so A's clean only wipes A's per-offloader build dir; B's
        compile artefacts under B's subtree are untouched. The
        receiver-side single-flight queue serializes the actual
        subprocess invocations regardless, but the per-offloader
        isolation is what makes the cross-offloader race a
        non-issue at the filesystem level.
        """
        await self._validate_configuration_boundary(configuration)
        if blocker := self._active_build_for(configuration):
            raise CommandError(
                ErrorCode.INVALID_ARGS,
                f"{blocker.job_type.value} job already in progress "
                f"for {configuration}; wait for it to finish or "
                f"cancel it before cleaning.",
            )
        local_job = self._create_job(configuration, JobType.CLEAN)
        enqueued = await self._enqueue(local_job)
        await self._fan_out_clean_to_connected_peers(configuration)
        return enqueued

    async def _fan_out_clean_to_connected_peers(self, configuration: str) -> None:
        """Queue one REMOTE clean job per connected approved peer.

        Reads the remote-build controller's RAM-canonical
        ``(_pairings, _open_peer_links)`` state via
        :meth:`OffloaderController.build_scheduler_snapshot`.
        Approved + connected peers get a job each; everything else
        is silently skipped (a PENDING row can't accept submits,
        a disconnected approved row would just FAIL on the runner's
        first ``_lookup_open_peer_link_client``).

        Fan-out is silent on the WS reply — the operator's
        ``firmware/clean`` call returns the local job; the remote
        jobs surface through the existing
        firmware-jobs subscribe-events stream the dashboard already
        consumes for in-flight job lists. A regression that lost
        the fan-out shows up as "I clicked Clean but my receiver
        still has the old build".
        """
        offloader = self._db.remote_build_offloader
        if offloader is None:
            return
        snapshot = offloader.build_scheduler_snapshot()
        # ``build_scheduler_snapshot`` ``dict(self._pairings)``-copies
        # on construction, so iteration is already isolated from a
        # concurrent unpair landing on a different loop tick.
        for pairing in snapshot.pairings.values():
            if pairing.status is not PeerStatus.APPROVED:
                continue
            if pairing.pin_sha256 not in snapshot.open_peer_links:
                continue
            remote_job = self._create_job(
                configuration,
                JobType.CLEAN,
                source=JobSource.REMOTE,
                source_pin_sha256=pairing.pin_sha256,
                source_label=pairing.label,
            )
            # ``supersede=False``: the fan-out batch is N+1 jobs
            # all sharing one ``configuration``, so default
            # supersede semantics ("cancel any prior active job
            # for this configuration") would cancel the local
            # clean we just queued plus every prior fan-out
            # sibling, leaving only the LAST peer's clean alive.
            # See ``_enqueue``'s docstring for the carve-out
            # rationale.
            await self._enqueue(remote_job, supersede=False)

    def _active_build_for(self, configuration: str) -> FirmwareJob | None:
        """Return any in-flight build-producing job on *configuration*.

        Filters ``_jobs`` by status (``_ACTIVE_JOB_STATUSES``) and
        type (``_BUILD_PRODUCING_JOB_TYPES``). Used by ``clean`` to
        reject rather than supersede when a destructive op would
        wipe artifacts the running job is producing.
        """
        for active in self._jobs.values():
            if active.configuration != configuration:
                continue
            if active.status not in _ACTIVE_JOB_STATUSES:
                continue
            if active.job_type in _BUILD_PRODUCING_JOB_TYPES:
                return active
        return None

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
        if force_local:
            source, pin_sha256, label = JobSource.LOCAL, "", ""
        else:
            source, pin_sha256, label = self._resolve_install_source(configuration)
        job = self._create_job(
            configuration,
            JobType.INSTALL,
            port=port,
            source=source,
            source_pin_sha256=pin_sha256,
            source_label=label,
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
                if force_local:
                    source, pin_sha256, label = JobSource.LOCAL, "", ""
                else:
                    source, pin_sha256, label = self._resolve_install_source(config)
                job = self._create_job(
                    config,
                    JobType.COMPILE,
                    source=source,
                    source_pin_sha256=pin_sha256,
                    source_label=label,
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
                source, pin_sha256, label = self._resolve_install_source(config)
                job = self._create_job(
                    config,
                    JobType.INSTALL,
                    port=port,
                    source=source,
                    source_pin_sha256=pin_sha256,
                    source_label=label,
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
        is_terminal = job.status in TERMINAL_JOB_STATUSES
        terminal_status = job.status.value if is_terminal else ""
        terminal_exit_code = job.exit_code
        terminal_error = job.error if is_terminal else None

        async def _send_initial(controls: StreamControls) -> None:
            for line in snapshot:
                await client.send_event(message_id, StreamEvent.OUTPUT, line)
            if is_terminal:
                await client.send_event(
                    message_id,
                    StreamEvent.RESULT,
                    {
                        "status": terminal_status,
                        "exit_code": terminal_exit_code,
                        # ``error`` carries the human-readable
                        # failure reason :func:`_fail_locally` /
                        # ``_finalize_terminal`` stamped on the job
                        # (e.g. ``"remote build: peer-link session
                        # lost (transport_error: ...)"``). The
                        # frontend install dialog surfaces this in
                        # its red error banner; without the field
                        # the banner falls back to a generic
                        # "Install failed." that misattributes a
                        # receiver-restart to a broken build env.
                        # ``None`` for successful jobs and for
                        # jobs from before the field was set;
                        # frontend treats both equivalently.
                        "error": terminal_error,
                    },
                )
                # No live drain — already-terminal job has nothing
                # more to deliver; end the stream so the helper
                # returns instead of parking on ``queue.get``.
                controls.end()

        def _handle_event(event: Event, controls: StreamControls) -> None:
            if event.event_type == EventType.JOB_OUTPUT:
                if event.data.get("job_id") == job_id:
                    controls.push(StreamEvent.OUTPUT, event.data["line"])
            elif event.event_type in TERMINAL_JOB_EVENTS:
                ev_job = event.data.get("job")
                if ev_job and getattr(ev_job, "job_id", None) == job_id:
                    status = getattr(ev_job, "status", "unknown")
                    status_val = status.value if hasattr(status, "value") else str(status)
                    controls.push_priority(
                        StreamEvent.RESULT,
                        {
                            "status": status_val,
                            "exit_code": getattr(ev_job, "exit_code", None),
                            "error": getattr(ev_job, "error", None),
                        },
                    )
                    controls.end()

        await stream_events(
            client=client,
            message_id=message_id,
            bus=self._db.bus,
            event_types=(EventType.JOB_OUTPUT, *TERMINAL_JOB_EVENTS),
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
                *TERMINAL_JOB_EVENTS,
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
        started_payload: JobLifecycleData = {"job": job}
        self._db.bus.fire(EventType.JOB_STARTED, started_payload)
        await self._persist_jobs()

        try:
            # Source-routed branch: REMOTE-source jobs dispatch via
            # peer-link to a paired receiver instead of running a
            # local subprocess. The receiver's ``OFFLOADER_JOB_*``
            # fan-out events drive the same lifecycle / output /
            # progress fires every local subscriber already
            # consumes — follow_job and the firmware-tasks UI don't
            # need to know whether the bytes are local or remote.
            if job.source is JobSource.REMOTE:
                await self._execute_remote_job(job)
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

            env = self._compose_subprocess_env(job)
            has_error_in_output = False
            # Captured at append time because the in-flight trim can
            # elide the offending line before the post-exit handler
            # runs. ``_check_error`` already had the line in hand
            # there; persisting the verdict here lets the post-exit
            # handler render a specific actionable message even
            # after a long noisy build trims the head.
            saw_no_esphome_module = False

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

            async with self._tracked_subprocess(
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
            ) as proc:
                # Honour a cancel that landed in the gap between
                # ``_verify_chip`` finishing and ``create_subprocess_exec``
                # returning — without this, an early Stop click during
                # the brief async window where ``_current_process`` was
                # ``None`` lets the install run to completion before the
                # post-``proc.wait()`` cancel check sees the flag.
                if job.job_id in self._cancel_requested:
                    await self._terminate_current_process()

                assert proc.stdout is not None  # type narrowing

                # ``iter_lines_with_progress`` splits on `\n` _or_ `\r`
                # so carriage-return-based in-place updates (esptool's
                # `Writing at 0x... (5%)\r`, PlatformIO's progress
                # bars) survive the pipe instead of getting buffered
                # until the next newline. Each chunk keeps its
                # trailing terminator so the frontend can decide
                # whether to append a new line or overwrite the last
                # one.
                async for line in iter_lines_with_progress(proc.stdout):
                    # Shared with the source-routed remote runner
                    # (``remote_runner._on_output``). The helper
                    # buffers + trims + fires ``JOB_OUTPUT`` and
                    # advances ``JOB_PROGRESS`` on a parseable
                    # percentage — same per-line bookkeeping
                    # whether the build's bytes come from this
                    # CPU or a paired receiver. ``_check_error``
                    # stays inline because it mutates the
                    # nonlocal ``has_error_in_output`` /
                    # ``saw_no_esphome_module`` flags the
                    # post-exit handler reads; remote builds
                    # surface a structured ``failed`` status from
                    # the receiver instead, so the stderr scrape
                    # only matters here.
                    _ingest_output_line(job, self._db.bus, line)
                    _check_error(line)

                exit_code = await proc.wait()
                job.exit_code = exit_code

            # If the user cancelled this job mid-run, the subprocess
            # exits non-zero (terminated by signal). Honour that
            # intent rather than reporting it as a generic failure.
            if job.job_id in self._cancel_requested:
                self._finalize_cancelled(job)
                _LOGGER.info("Job %s cancelled mid-run (exit %s)", job.job_id, exit_code)
            else:
                success = exit_code == 0 and not has_error_in_output
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

                # ``_finalize_terminal`` runs the mark + slot-
                # release + fire sequence in the order the
                # ``queue_status`` broadcaster needs (see helper
                # docstring for the regression context).
                self._finalize_terminal(job, JobStatus.COMPLETED if success else JobStatus.FAILED)
                _LOGGER.info(
                    "Job %s %s (exit code %s)",
                    job.job_id,
                    job.status,
                    exit_code,
                )

        except asyncio.CancelledError:
            # ``_tracked_subprocess`` already terminated the spawn
            # on its way out; this branch only needs to finalise
            # the job model and fire the event.
            self._finalize_cancelled(job)
            _LOGGER.info("Job %s cancelled (runner shutdown)", job.job_id)
            raise
        except Exception as exc:
            # If a cancel was requested before this exception escaped,
            # honour it as CANCELLED instead of FAILED. The
            # ``_verify_chip`` early-cancel path raises ``ValueError``
            # to short-circuit the install — without this branch
            # that error would be reported as a generic failure
            # rather than the user-driven cancel it actually is.
            if job.job_id in self._cancel_requested:
                self._finalize_cancelled(job)
                _LOGGER.info("Job %s cancelled before subprocess wait: %s", job.job_id, exc)
            else:
                job.error = str(exc)
                self._finalize_terminal(job, JobStatus.FAILED)
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

    async def _execute_remote_job(self, job: FirmwareJob) -> None:
        """
        Run a ``JobSource.REMOTE`` job by dispatching through peer-link.

        Reads ``source_pin_sha256`` off *job*, looks up the live
        :class:`PeerLinkClient` through the remote-build
        controller, bundles the YAML via the ``esphome bundle``
        subprocess, dispatches ``submit_job(target="compile")``,
        then translates receiver-side ``OFFLOADER_JOB_OUTPUT`` /
        ``OFFLOADER_JOB_STATE_CHANGED`` events into the same
        local ``JOB_OUTPUT`` / ``JOB_PROGRESS`` /
        ``JOB_<terminal>`` fires the local subprocess path emits.
        ``follow_job`` and the firmware-tasks UI consume one
        event stream regardless of which CPU compiled the bytes.

        Dispatches by ``job.job_type``:

        * :attr:`JobType.COMPILE` — wait for the receiver's
          terminal frame, finalise based on the wire status.
        * :attr:`JobType.UPLOAD` / :attr:`JobType.INSTALL` —
          same compile dispatch (per § Transparent install
          flow's load-bearing "receiver only ever compiles"
          policy), but on receiver-completed pull the
          artifacts back via ``download_artifacts`` and run a
          local ``esphome upload --file <staged>`` subprocess
          to flash the device. The local flash step shares the
          ``_tracked_subprocess`` plumbing the LOCAL path uses
          so cancel SIGTERM lands on the upload chain the same
          way.

        Other job types (``CLEAN`` / ``RENAME`` /
        ``RESET_BUILD_ENV``) are rejected at the runner's top
        because the receiver-side ``submit_job`` contract is
        compile-only — these don't have a corresponding wire
        flow.

        Terminal states are mapped through the same helpers the
        local path uses (``_mark_job_terminal`` /
        ``_finalize_cancelled``), so the outer
        ``_execute_job``'s ``finally`` runs the shared
        ``_trim_job_output`` / ``_prune_history`` / persist
        sequence regardless of which branch produced the
        terminal status.
        """
        await run_remote_job(self, job)

    @asynccontextmanager
    async def _tracked_subprocess(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[asyncio.subprocess.Process]:
        """
        Spawn a subprocess that's visible to ``firmware/cancel``.

        Required for every ``create_subprocess_exec`` call in the
        runner path — both the main install/upload spawn in
        ``_execute_job`` and pre-flight probes like
        ``_verify_chip``. Setting ``_current_process`` is what lets
        a concurrent ``firmware/cancel`` actually land SIGTERM on
        the running spawn; a direct ``create_subprocess_exec`` call
        without this registration silently regresses the
        issue-#136 fix — the cancel handler walks
        ``_current_process``, no-ops on ``None``, the user clicks
        Stop, nothing visible happens, and the orphaned subprocess
        runs to completion in the background.

        Two cleanup contracts on exit:

        - Normal exit / non-cancellation exception: restore the
          prior ``_current_process`` value so nested usage (a
          future spawn site that itself wraps another) doesn't
          accidentally null out an outer registration.
        - ``asyncio.CancelledError`` (runner-task shutdown):
          terminate the spawn before propagating, so the build
          can't outlive the runner that started it. The outer
          ``except asyncio.CancelledError`` in ``_execute_job``
          handles the job-finalisation half and relies on this
          helper for the terminate.

        Pairs with ``_raise_if_cancelled`` — wrap each spawn, then
        call the helper after to short-circuit if the cancel landed
        between this subprocess and the next one.
        """
        proc = await create_subprocess_exec(*args, **kwargs)
        prev = self._current_process
        self._current_process = proc
        try:
            yield proc
        except asyncio.CancelledError:
            # Runner-shutdown cancellation: the runner task itself
            # was cancelled (vs. a user-driven ``firmware/cancel``,
            # which calls ``_terminate_current_process`` from the
            # cancel handler directly). Reuse the same group-aware
            # termination helper here so SIGTERM walks the whole
            # process group (esphome → platformio → gcc / esptool).
            # ``proc.terminate()`` would only signal the python
            # parent — on POSIX with ``start_new_session=True``
            # that orphans the child tree and the build keeps
            # running until the children finish on their own.
            await self._terminate_current_process()
            raise
        finally:
            self._current_process = prev

    def _finalize_terminal(self, job: FirmwareJob, status: JobStatus) -> None:
        """Stamp *job* terminal, release the runner slot, fire the matching event.

        The three-step "this job is over" sequence every
        finalisation site has to run, bundled so a future call
        site can't accidentally skip one. Steps in order:

        1. :func:`_mark_job_terminal` stamps ``status`` +
           ``completed_at`` on the model (raises on a
           non-terminal *status* — preserves the existing
           loud-fail guard).
        2. Release the runner slot (``_current_job`` /
           ``_current_process``) if *job* holds it. Guarded on
           ``is job`` so the QUEUED-cancel path in :meth:`cancel`
           (where ``_current_job`` belongs to whatever's actually
           running) doesn't evict the wrong job.
        3. Fire the matching lifecycle event
           (``JOB_COMPLETED`` / ``JOB_FAILED`` / ``JOB_CANCELLED``)
           on the bus.

        Step 2 has to land *before* step 3: the ``queue_status``
        broadcaster
        (:meth:`ReceiverController._on_firmware_queue_transition`)
        reads :meth:`queue_status_snapshot` *synchronously*
        inside the fire and needs to see the post-terminal idle
        state. Without the prior release the snapshot reports
        ``running=True``, the offloader's ``_peer_queue_status``
        cache freezes there, and
        :func:`helpers.build_scheduler.pick_build_path` rejects
        the pairing on every subsequent install — the silent
        LOCAL-fallback regression after the first remote build.
        The ``finally`` block in :meth:`_execute_job` still
        re-clears both fields as a backstop for exception paths
        that bypass this helper.

        Callers that want to ride a payload field (e.g.
        ``job.error = "..."``) into the event must set it on the
        job *before* invoking this helper — the broadcast reads
        whatever's on the model at fire time.
        """
        _mark_job_terminal(job, status)
        if self._current_job is job:
            self._current_job = None
            self._current_process = None
        payload: JobLifecycleData = {"job": job}
        self._db.bus.fire(_STATUS_TO_TERMINAL_EVENT[status], payload)

    def _finalize_cancelled(self, job: FirmwareJob) -> None:
        """
        Run the runtime-cancel finalisation: discard, mark, fire.

        Drops the id from ``_cancel_requested`` (so a subsequent
        re-queue starts clean) then routes through
        :meth:`_finalize_terminal` for the mark + slot-release +
        fire sequence. Each call site adds its own log line so
        the message can name the phase that was cancelled.

        Doesn't cover the QUEUED-cancel path in ``cancel`` itself —
        that one also runs ``_prune_history`` + ``_persist_jobs``
        because the runner never sees the job, and inlining those
        here would couple the runtime-cancel sites to disk I/O
        they don't otherwise need.
        """
        self._cancel_requested.discard(job.job_id)
        self._finalize_terminal(job, JobStatus.CANCELLED)

    def _raise_if_cancelled(self, job: FirmwareJob, phase: str) -> None:
        """
        Short-circuit a runner phase if a cancel landed mid-phase.

        Called between subprocess spawns so a cancel that came in
        while one pre-flight was running stops the next one from
        starting. Raises ``ValueError`` so ``_execute_job``'s
        cancel-aware ``except Exception`` branch finalises the job
        as ``CANCELLED`` (vs. the bare ``FAILED`` path used for
        unrelated exceptions). ``phase`` shows up in the error
        message to make the cause clear in the log.
        """
        if job.job_id in self._cancel_requested:
            msg = f"Cancelled during {phase}"
            raise ValueError(msg)

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

    async def _verify_chip(self, job: FirmwareJob) -> None:
        """
        Verify the chip on the serial port matches the device config.

        Runs ``esptool chip-id`` to detect the actual chip, then
        compares against the chip variant recorded in StorageJSON.
        Raises ValueError on mismatch so the job fails early with a
        clear error message.

        Reads from ``StorageJSON.target_platform`` (the upstream-
        canonical chip variant — ``ESP32S3`` / ``ESP32C3`` / plain
        ``ESP8266``) rather than ``Device.target_platform`` (which
        carries the lowercase platform *key*, e.g. ``esp32`` for
        every ESP32 family member, and would false-positive on a
        chip-vs-variant mismatch). Skipped when StorageJSON is
        absent — pre-compile installs have no compile-time truth
        to compare against, and esphome's own flash error covers
        the wrong-chip case there.

        The verify subprocess is registered as ``_current_process``
        for the duration of its run so an early ``firmware/cancel``
        — typical when the user picked the wrong serial port and
        esptool is hanging waiting for a device that won't answer
        — actually lands on the spawned esptool process, instead
        of no-op'ing because the main install hadn't been spawned
        yet. ``start_new_session=True`` puts the process in its
        own group so the SIGTERM signal walks the whole tree the
        same way the main install spawn site does.
        """
        if not job.port or job.port.upper() == "OTA" or not job.port.startswith("/dev"):
            return  # only check serial ports

        loop = asyncio.get_running_loop()
        storage = await loop.run_in_executor(
            None, lambda: StorageJSON.load(resolve_storage_path(job.configuration))
        )
        if storage is None or not storage.target_platform:
            return  # never compiled or no platform recorded — nothing to verify

        expected_platform = storage.target_platform.lower()

        async with self._tracked_subprocess(
            sys.executable,
            "-m",
            "esptool",
            "--port",
            job.port,
            "chip-id",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        ) as proc:
            assert proc.stdout is not None  # type narrowing
            output = (await proc.stdout.read()).decode("utf-8", errors="replace")
            await proc.wait()

        # Honour an early cancel that arrived during chip detection
        # (the main install hasn't spawned yet, so the post-wait
        # check below in ``_execute_job`` would otherwise let the
        # full install run before reporting CANCELLED). Reusing
        # the ``ValueError`` shape keeps the error path identical
        # to a chip mismatch.
        self._raise_if_cancelled(job, "chip verification")

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

    def _compose_subprocess_env(self, job: FirmwareJob) -> dict[str, str]:
        """Return the env dict for *job*'s ``esphome`` subprocess.

        Layers, in order:

        1. ``os.environ`` (inherits the dashboard's deployment-mode
           context: ``ESPHOME_DATA_DIR`` on the HA addon, etc.).
        2. :data:`ESPHOME_SUBPROCESS_ENV` (force ANSI colour /
           unbuffered output regardless of TTY — see the constant's
           docstring for the rationale).
        3. For a receiver-side remote-build job (configuration
           parses through
           :func:`helpers.remote_build_layout.parse_from_configuration`
           — i.e. the YAML lives under
           ``.esphome/.remote_builds/<dashboard_id>/<device>/``):
           pin ``ESPHOME_DATA_DIR`` to the per-build subtree so
           every per-config artefact (storage sidecar, idedata
           cache, build directory, PlatformIO project) lands under
           one ``(dashboard_id, device)``-keyed directory.

        Without step 3 the subprocess would inherit the
        dashboard's own ``ESPHOME_DATA_DIR`` and write storage /
        build / idedata into the same keyspace as the user's
        local builds — same-basename collisions across paired
        offloaders silently mix builds. The override pins each
        offloader's remote builds to a per-dashboard subdirectory
        of ``CORE.data_dir``
        (``<config_dir>/.esphome/.remote_builds/<id>/.esphome``
        in default mode, ``/data/.remote_builds/<id>/.esphome``
        on the HA addon). Anchoring on ``CORE.data_dir`` (not on
        ``settings.config_dir``) keeps the multi-GB toolchain +
        build cache off the user's ``/config`` mount on the HA
        addon, where it lives on the addon's per-instance data
        volume instead.

        Reclaim semantics (see
        :meth:`RemoteBuildPath.data_dir` for the full notes):
        the 6c TTL sweep
        (:func:`helpers.remote_build_cleanup.sweep_remote_builds`)
        still walks ``config_dir / REMOTE_BUILDS_SUBDIR`` and
        reclaims cold per-device subtrees (the YAML extract +
        bundle sibling) via :meth:`RemoteBuildPath.subtree`.
        The shared per-dashboard ``.esphome/`` under
        ``CORE.data_dir`` is intentionally not swept — that's
        what keeps the toolchain warm across submits — at the
        cost of per-device ``build/<name>/`` /
        ``storage/<basename>.json`` / ``idedata/<name>.json``
        becoming orphaned when their subtree gets reclaimed.
        A future cleanup pass keyed on "no devices remain
        under this dashboard_id" will reclaim the whole shared
        tree on unpair / TTL expiry.

        Factored out of :meth:`_execute_job` so the receiver-side
        env override is unit-testable without standing up a real
        subprocess.
        """
        env = {**os.environ, **ESPHOME_SUBPROCESS_ENV}
        remote_build_path = parse_remote_build_path(job.configuration)
        if remote_build_path is not None:
            env["ESPHOME_DATA_DIR"] = str(remote_build_path.data_dir(Path(CORE.data_dir)))
        return env

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
            # ``clean-all`` takes the config *directory* as its
            # positional, not a YAML file. ``reset_build_env`` queues
            # with ``configuration=""`` so ``rel_path("")`` resolves
            # back to the config_dir at the call site — same shape
            # as the legacy dashboard's ``EsphomeCleanAllHandler``.
            JobType.RESET_BUILD_ENV: "clean-all",
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
        if job.job_type not in _OTA_ADDRESS_CACHE_JOB_TYPES or self._db.devices is None:
            return []
        # ``rename``'s ``port`` is the post-rename re-install target;
        # the inner ``esphome run`` against the *old* address is
        # always OTA, so skip the gate with ``None``.
        port: str | None = None if job.job_type == JobType.RENAME else job.port
        return self._db.devices.get_ota_address_cache_args(job.configuration, port)

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
        source: JobSource = JobSource.LOCAL,
        source_pin_sha256: str = "",
        source_label: str = "",
        device_name: str = "",
        device_friendly_name: str = "",
    ) -> FirmwareJob:
        """Create a new job and add it to the in-memory map.

        Caller is responsible for having validated ``configuration``
        first via ``_validate_configuration_boundary`` — keeping it
        async-only lets the validation run in an executor without
        making this helper async too.

        ``remote_peer`` is the offloader's ``dashboard_id`` when
        this job came in via the peer-link ``submit_job`` flow
        (issue #106), empty otherwise. ``remote_job_id``
        is the offloader's submit-tagged ``job_id`` from the
        same flow; the receiver-side ``job_id`` above is
        generated independently so the two id-spaces don't
        collide. ``remote_peer_label`` is the offloader's
        display label (:attr:`StoredPeer.label`) snapshotted at
        submit time so the receiver's firmware-tasks UI can
        render "from {label}" without a separate lookup —
        symmetric to ``source_label`` on the offloader side.

        ``device_name`` / ``device_friendly_name`` are the
        ``esphome.name`` / ``esphome.friendly_name`` the
        offloader sends on the :class:`SubmitJobFrameData`
        header — the offloader already has both off its local
        Device scanner at install time, so the receiver doesn't
        re-parse the bundled YAML. Peer-controlled at the wire
        boundary; the receive-side handler
        (:meth:`SubmitJobReceiver.handle_submit_job`) coerces +
        length-caps them via ``_coerce_display_field`` before
        passing them here. Empty for locally-submitted jobs
        (the dashboard's own Device list already carries the
        friendly name).

        ``source`` / ``source_pin_sha256`` / ``source_label`` are
        the offloader-side dispatch-origin fields: ``LOCAL`` for
        jobs the offloader compiles itself, ``REMOTE`` for jobs
        the offloader dispatches to a paired receiver via the
        source-routed runner. ``source_pin_sha256``
        identifies the receiver; ``source_label`` is the display
        name. Defaults make every existing call site continue to
        produce LOCAL jobs.
        """
        job = FirmwareJob(
            job_id=uuid4().hex[:12],
            configuration=configuration,
            job_type=job_type,
            created_at=datetime.now(UTC).isoformat(),
            port=port,
            new_name=new_name,
            remote_peer=remote_peer,
            remote_peer_label=remote_peer_label,
            remote_job_id=remote_job_id,
            source=source,
            source_pin_sha256=source_pin_sha256,
            source_label=source_label,
            device_name=device_name,
            device_friendly_name=device_friendly_name,
        )
        self._jobs[job.job_id] = job
        return job

    def _resolve_install_source(self, configuration: str) -> tuple[JobSource, str, str]:
        """Pick LOCAL or REMOTE for *configuration*; return ``(source, pin, label)``.

        Pure sync helper. Returns LOCAL when remote-build isn't
        wired up yet (firmware-queue restart-recovery can fire
        before remote-build's ``start()``).
        """
        offloader = self._db.remote_build_offloader
        if offloader is None:
            return JobSource.LOCAL, "", ""
        decision = pick_build_path(offloader.build_scheduler_snapshot())
        if decision.path is not BuildPath.REMOTE or decision.pin_sha256 is None:
            return JobSource.LOCAL, "", ""
        pairing = offloader.get_pairing(decision.pin_sha256)
        if pairing is None:
            # Scheduler picked a pin that's no longer paired (race
            # vs an ``unpair`` on the same loop tick).
            return JobSource.LOCAL, "", ""
        return JobSource.REMOTE, pairing.pin_sha256, pairing.label

    async def _enqueue(self, job: FirmwareJob, *, supersede: bool = True) -> FirmwareJob:
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

        ``supersede=False`` opts out of the cancel-by-configuration
        step. Used by the ``firmware/clean`` fan-out: the local job
        enqueues with supersede=True (cancelling any prior in-flight
        work on this device, the right "make this device idle"
        behaviour), then the per-peer remote fan-out jobs enqueue
        with supersede=False so the fan-out batch doesn't cancel
        its own siblings or the just-queued local job. Without this
        carve-out, every successive ``_enqueue`` in the fan-out
        loop would cancel its predecessors and only the LAST peer's
        clean would actually run; see esphome/device-builder#608
        review for the failure trace. The carve-out is safe because
        the fan-out is a coordinated batch from one operator click;
        if the user clicks clean a second time, the next batch's
        LOCAL enqueue (supersede=True) cancels every member of the
        prior batch in one pass.

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
        queued_payload: JobLifecycleData = {"job": job}
        self._db.bus.fire(EventType.JOB_QUEUED, queued_payload)
        if supersede and job.configuration:
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
            if active.status not in _ACTIVE_JOB_STATUSES:
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
            and j.status in _ACTIVE_JOB_STATUSES
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
        terminal_states = TERMINAL_JOB_STATUSES

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
                if job.status in _ACTIVE_JOB_STATUSES:
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
