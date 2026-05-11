"""
Source-routed runner branch for ``JobSource.REMOTE`` firmware jobs.

Lives in its own module so ``controller.py`` stays focused on the
local subprocess pipeline + WS surface. The branch is invoked
from :meth:`FirmwareController._execute_job` when the job's
``source`` field is ``REMOTE``, after ``JOB_STARTED`` has fired
and before chip-verify; it returns once the receiver has emitted
a terminal ``OFFLOADER_JOB_STATE_CHANGED`` (or a translatable
failure path has been finalised locally).

The receiver doesn't know about the offloader-side
:class:`FirmwareJob` — it tracks its own row keyed by its own
``job_id``, and echoes the offloader's id back on every
fan-out frame so the offloader can correlate. We use
``job.job_id`` (the offloader-side id) as the match key on
inbound :class:`OffloaderJobOutputData` /
:class:`OffloaderJobStateChangedData` events.

Listener attach happens **before** ``submit_job`` is sent so an
immediate ``running`` / ``output`` frame from the receiver
can't outrace our subscription. The listener bucket is
process-wide; a stray frame from a different in-flight remote
job lands in a different match-id / pin combination and is
filtered out at the callback boundary.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.config_bundle import BundleBuildError, build_yaml_bundle
from ...helpers.subprocess import iter_lines_with_progress
from ...models import (
    EventType,
    FirmwareJob,
    JobLifecycleData,
    JobStatus,
    JobType,
    OffloaderJobOutputData,
    OffloaderJobStateChangedData,
    OffloaderPeerLinkClosedData,
)
from ..remote_build.artifacts_tarball import UnpackArtifactsError, extract_firmware_bin
from ..remote_build.peer_link_client import (
    DownloadArtifactsError,
    PeerLinkNoSessionError,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
)
from .constants import ESPHOME_SUBPROCESS_ENV
from .helpers import _ingest_output_line, _mark_job_terminal

if TYPE_CHECKING:
    from ...helpers.event_bus import Event, EventBus
    from ..remote_build.peer_link_client import PeerLinkClient
    from .controller import FirmwareController

_LOGGER = logging.getLogger(__name__)

# Terminal receiver-side statuses on
# :class:`OffloaderJobStateChangedData`. Mirror of
# :data:`TERMINAL_JOB_STATUSES` but on the wire literal rather
# than the local enum — receiver-side fan-out emits the lower-
# case string per :class:`JobStateChangedFrameData`'s
# ``Literal`` union.
_TERMINAL_WIRE_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


async def run_remote_job(
    controller: FirmwareController,
    job: FirmwareJob,
) -> None:
    """
    Run a REMOTE-source firmware job and finalise *job* on the offloader bus.

    Caller (``FirmwareController._execute_job``) has already
    set ``status = RUNNING`` and fired ``JOB_STARTED``; this
    function is responsible for the entire run-and-finalise
    middle and leaves the outer ``finally`` block to clear
    ``_current_job`` / persist.

    Dispatches by ``job.job_type``:

    * :attr:`JobType.COMPILE` — submit_job(target="compile"),
      wait for the receiver's terminal frame, finalise based
      on the wire status.
    * :attr:`JobType.UPLOAD` / :attr:`JobType.INSTALL` — same
      compile dispatch (per § Transparent install flow's
      load-bearing "receiver only ever compiles" policy), but
      on receiver-completed pull the artifacts back via
      ``download_artifacts`` and run a local
      ``esphome upload --file <staged_firmware.bin>``
      subprocess to flash the device. INSTALL and UPLOAD
      share this shape — the difference between the two on
      the local subprocess path is "compile-then-upload" vs
      "upload existing artifact", and here the receiver
      already did the compile half.

    Other job types (``CLEAN`` / ``RENAME`` / ``RESET_BUILD_ENV``)
    are rejected at the top because the receiver-side
    ``submit_job`` contract is compile-only and these don't
    have a corresponding wire flow.
    """
    if job.job_type not in (JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL):
        _fail_locally(
            controller,
            job,
            error=f"remote source supports COMPILE/UPLOAD/INSTALL only (got {job.job_type.value})",
        )
        return

    if not job.source_pin_sha256:
        _fail_locally(
            controller,
            job,
            error="remote source missing source_pin_sha256",
        )
        return

    bus = controller.bus
    pin = job.source_pin_sha256
    target_job_id = job.job_id
    loop = asyncio.get_running_loop()
    terminal: asyncio.Future[OffloaderJobStateChangedData] = loop.create_future()
    # Set when the peer-link session backing this job's
    # receiver closes. Distinct from ``terminal`` because the
    # receiver hasn't given us a structured terminal status —
    # we have to synthesise the lost-session failure on the
    # offloader side. ``_await_terminal`` waits on whichever
    # fires first.
    session_lost: asyncio.Future[OffloaderPeerLinkClosedData] = loop.create_future()
    # Set by ``FirmwareController.cancel`` when the user clicks
    # Stop. Registering it on the controller lets the cancel
    # handler signal the parked runner instantly instead of
    # waiting for a polling cadence — the runner parks on
    # ``asyncio.wait({terminal, session_lost, cancel_event_task})``
    # and wakes on whichever fires first.
    cancel_event = asyncio.Event()
    controller._cancel_events[job.job_id] = cancel_event
    # A cancel that landed during ``_execute_job``'s pre-runner
    # phase (``_current_job = job`` is set before the persist
    # await, so the cancel handler accepts the request and
    # writes to ``_cancel_requested`` — but the runner hasn't
    # yet installed its event, so the handler's
    # ``_cancel_events.get(job_id)`` returns ``None`` and the
    # ``set()`` is skipped). Replay the late wake here so the
    # runner doesn't park on an event that will never fire.
    if job.job_id in controller._cancel_requested:
        cancel_event.set()

    def _is_ours(data: OffloaderJobOutputData | OffloaderJobStateChangedData) -> bool:
        """Return True if *data* belongs to this runner's (pin, job_id) pair."""
        return data["pin_sha256"] == pin and data["job_id"] == target_job_id

    def _on_output(event: Event[OffloaderJobOutputData]) -> None:
        if not _is_ours(event.data):
            return
        _ingest_output_line(job, bus, event.data["line"])

    def _on_state(event: Event[OffloaderJobStateChangedData]) -> None:
        data = event.data
        if not _is_ours(data):
            return
        if data["status"] in _TERMINAL_WIRE_STATUSES and not terminal.done():
            terminal.set_result(data)

    def _on_session_closed(event: Event[OffloaderPeerLinkClosedData]) -> None:
        # Pin-only filter — session-close events don't carry a
        # job_id (the close brings down every in-flight job on
        # the link). ``_is_ours`` keys on job_id too, so this
        # one stays inline.
        data = event.data
        if data["pin_sha256"] != pin or session_lost.done():
            return
        session_lost.set_result(data)

    try:
        with (
            bus.listening([EventType.OFFLOADER_JOB_OUTPUT], _on_output),
            bus.listening([EventType.OFFLOADER_JOB_STATE_CHANGED], _on_state),
            bus.listening([EventType.OFFLOADER_PEER_LINK_CLOSED], _on_session_closed),
        ):
            try:
                await _dispatch_and_drive(
                    controller=controller,
                    job=job,
                    terminal=terminal,
                    session_lost=session_lost,
                    cancel_event=cancel_event,
                )
            except asyncio.CancelledError:
                # Runner-task shutdown (controller stop). Mirror the
                # local subprocess path's contract: finalise the job
                # as CANCELLED so subscribers see a terminal event,
                # then re-raise to let the outer runner unwind.
                controller._finalize_cancelled(job)
                _LOGGER.info("Remote job %s cancelled (runner shutdown)", job.job_id)
                raise
    finally:
        # Clean up the registration so a future job reusing the
        # id (theoretical — uuid4 collision aside) doesn't
        # signal a stale event.
        controller._cancel_events.pop(job.job_id, None)


async def _dispatch_and_drive(  # noqa: PLR0911
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    terminal: asyncio.Future[OffloaderJobStateChangedData],
    session_lost: asyncio.Future[OffloaderPeerLinkClosedData],
    cancel_event: asyncio.Event,
) -> None:
    """Build the bundle, submit, then wait for the receiver's terminal frame.

    Split out from :func:`run_remote_job` so the
    listener attach / detach lives in one ``with`` block at the
    outer call site — every early-return failure path here
    still releases the bus subscriptions.
    """
    loop = asyncio.get_running_loop()
    yaml_path = await loop.run_in_executor(
        None, controller._db.settings.rel_path, job.configuration
    )

    try:
        bundle_bytes = await build_yaml_bundle(yaml_path)
    except FileNotFoundError:
        _fail_locally(
            controller,
            job,
            error=f"remote build: configuration not found: {job.configuration}",
        )
        return
    except BundleBuildError as exc:
        _fail_locally(
            controller,
            job,
            error=f"remote build: bundle failed: {exc.output or exc}",
        )
        return

    remote_build = controller._db.remote_build
    if remote_build is None:
        _fail_locally(
            controller,
            job,
            error="remote build: controller not initialised",
        )
        return
    try:
        client = remote_build._lookup_open_peer_link_client(
            job.source_pin_sha256, label="firmware_remote"
        )
    except CommandError as exc:
        _fail_locally(
            controller,
            job,
            error=f"remote build: receiver not reachable: {exc.message}",
        )
        return

    try:
        ack = await client.submit_job(
            job_id=job.job_id,
            configuration_filename=job.configuration,
            target="compile",
            bundle_bytes=bundle_bytes,
        )
    except (PeerLinkNoSessionError, SubmitJobTimeoutError, SubmitJobSessionLostError) as exc:
        _fail_locally(
            controller,
            job,
            error=f"remote build: dispatch failed: {exc}",
        )
        return

    if not ack["accepted"]:
        reason = ack.get("reason", "no reason given")
        _fail_locally(
            controller,
            job,
            error=f"remote build: receiver rejected job: {reason}",
        )
        return

    wire_status = await _await_terminal(
        controller=controller,
        job=job,
        terminal=terminal,
        session_lost=session_lost,
        cancel_event=cancel_event,
    )
    if wire_status != "completed":
        # ``_await_terminal`` already finalised the job (cancel
        # / failed / session-lost / explicit-cancelled). Nothing
        # left to do.
        return

    # Receiver compiled successfully. For COMPILE jobs that's
    # the whole job; for UPLOAD / INSTALL we still owe the
    # local flash step using the receiver's bytes.
    if job.job_type is JobType.COMPILE:
        # Stamp ``exit_code=0`` because the remote compile
        # didn't run a local subprocess. The legacy
        # ``follow_job`` framing coerces ``None`` to a
        # failure code (``1``), so a missing stamp would
        # land a successful compile as a failure on the wire.
        job.exit_code = 0
        _finalize_success(controller, job)
        return
    await _fetch_and_run_local_upload(controller=controller, job=job, client=client)


async def _await_terminal(
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    terminal: asyncio.Future[OffloaderJobStateChangedData],
    session_lost: asyncio.Future[OffloaderPeerLinkClosedData],
    cancel_event: asyncio.Event,
) -> str | None:
    """
    Wait for the receiver's terminal state, translating local cancel to the wire.

    Parks on ``asyncio.wait({terminal, session_lost,
    cancel_event.wait()}, return_when=FIRST_COMPLETED)`` —
    event-driven, no polling cadence. The cancel handler
    (``FirmwareController.cancel``) signals *cancel_event*
    when the user clicks Stop, so the runner wakes
    instantly instead of waiting up to half a second for a
    poll iteration. ``session_lost`` is the synthetic
    failure path the offloader uses when the peer-link
    session closes before the receiver can emit a terminal
    frame; without it the wait would deadlock on a dead
    receiver.

    Returns the receiver-side wire status string (``"completed"``
    / ``"failed"`` / ``"cancelled"``) when the receiver's frame
    arrived AND the local side hasn't already finalised the
    job for some other reason. Returns ``None`` when the local
    side already wrote a terminal status (cancel, session loss,
    failed, receiver-side cancelled) — the caller has nothing
    left to do. Specifically: ``"completed"`` is the *only*
    return value the caller must act on (it owes the local
    flash step for UPLOAD / INSTALL); every other terminal
    state has been finalised here.
    """
    cancel_sent = False
    # ``asyncio.wait`` is invariant on its element type; the
    # heterogeneous awaitables (two TypedDict futures + one
    # Event-wait coroutine wrapped as a Task) are widened to
    # ``Any`` at the call so mypy doesn't reject the mix.
    cancel_wait = asyncio.get_running_loop().create_task(cancel_event.wait())
    waiters: list[asyncio.Future[Any]] = [terminal, session_lost, cancel_wait]
    try:
        while not terminal.done():
            if session_lost.done():
                closed = session_lost.result()
                reason = closed["reason"]
                detail = closed["error_detail"]
                text = f"{reason}: {detail}" if detail else reason
                _fail_locally(
                    controller,
                    job,
                    error=f"remote build: peer-link session lost ({text})",
                )
                return None
            if cancel_event.is_set() and not cancel_sent:
                cancel_sent = True
                if not await _send_cancel_or_finalise(controller, job):
                    return None
                # After dispatching the wire cancel we only care
                # about ``terminal`` / ``session_lost`` — the
                # cancel-event signal has already done its job.
                waiters = [terminal, session_lost]
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        if not cancel_wait.done():
            cancel_wait.cancel()

    data = terminal.result()
    if job.job_id in controller._cancel_requested:
        # User cancel beat the receiver's terminal frame to the
        # loop (receiver completed / failed while our cancel was
        # in flight). Mirror the local subprocess path: user
        # intent wins, finalise as CANCELLED regardless of the
        # status we received.
        controller._finalize_cancelled(job)
        return None
    status = data["status"]
    if status == "cancelled":
        controller._finalize_cancelled(job)
        return None
    if status == "failed":
        # Receiver-supplied error text rides into ``job.error``;
        # an empty ``error_message`` (older receiver, internal
        # bug) falls back to a generic string so subscribers
        # always see a non-empty reason.
        _fail_locally(
            controller,
            job,
            error=data["error_message"] or "remote build failed",
        )
        return None
    # ``completed`` — the only status the caller must act on.
    # Don't finalise here; the caller owes a local flash step
    # for UPLOAD / INSTALL.
    return status


async def _fetch_and_run_local_upload(
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    client: PeerLinkClient,
) -> None:
    """
    Pull the receiver's compile artifacts and flash the device locally.

    Called once the receiver returned ``completed`` for an
    ``UPLOAD`` / ``INSTALL`` job. The transparent install
    contract says the offloader owns the flash step — only
    the *compile* hops to the receiver. So:

    1. Fetch the artifact tarball via
       :meth:`PeerLinkClient.download_artifacts`.
    2. Extract ``firmware.bin`` to a per-run tmpdir (the
       only piece the OTA / web_server flash paths need; the
       multi-image set required for ESP32 wired flash isn't
       supported in 7a-3 — serial REMOTE installs were
       rejected at the install handler).
    3. Spawn ``esphome upload --device <port> --file
       <staged>`` through :meth:`FirmwareController._tracked_subprocess`
       so the cancel handler's SIGTERM lands on the subprocess
       if the user clicks Stop mid-upload.
    4. Stream stdout through :func:`helpers._ingest_output_line`
       — same per-line bookkeeping (buffer / trim / fire
       ``JOB_OUTPUT`` + ``JOB_PROGRESS``) every local
       subscriber already consumes.
    5. Finalise based on exit code + cancel state, same shape
       the local subprocess path uses.

    Wire / unpack failures and a non-zero upload exit fail
    the job locally with ``JOB_FAILED`` (or ``JOB_CANCELLED``
    via the cancel-aware ``_fail_locally`` when the user
    raced a Stop).
    """
    try:
        packed = await client.download_artifacts(job_id=job.job_id)
    except (
        PeerLinkNoSessionError,
        SubmitJobSessionLostError,
        DownloadArtifactsError,
    ) as exc:
        _fail_locally(
            controller,
            job,
            error=f"remote build: download_artifacts failed: {exc}",
        )
        return

    # Extract firmware.bin from the receiver's gzipped tarball.
    # The receiver-side packer guarantees ``firmware.bin`` is
    # always present (see ``ArtifactsDownloadSender``); a
    # missing entry means the wire shape drifted, so surface
    # as a clean error rather than letting the upload step
    # silently flash whatever was at the tmpdir path.
    try:
        firmware_bytes = await asyncio.get_running_loop().run_in_executor(
            None, extract_firmware_bin, packed.tarball
        )
    except UnpackArtifactsError as exc:
        _fail_locally(
            controller,
            job,
            error=f"remote build: tarball: {exc}",
        )
        return

    # Honour a cancel that arrived between the receiver's
    # completed frame and us getting here — no point staging
    # bytes or spawning a flash subprocess for a job the user
    # already aborted. ``_fail_locally`` is cancel-aware and
    # routes through ``_finalize_cancelled`` in this case.
    if job.job_id in controller._cancel_requested:
        controller._finalize_cancelled(job)
        return

    bus = controller.bus
    loop = asyncio.get_running_loop()
    yaml_path = await loop.run_in_executor(
        None, controller._db.settings.rel_path, job.configuration
    )

    # ``tempfile.TemporaryDirectory`` ctor calls
    # :func:`os.mkdir` synchronously — blockbuster catches
    # that on CI. Use :func:`tempfile.mkdtemp` via an executor
    # and clean up by hand in the ``finally`` so the blocking
    # syscalls (``os.mkdir`` / ``shutil.rmtree``) never run on
    # the event loop.
    tmpdir = await loop.run_in_executor(None, tempfile.mkdtemp, "", "esphome-remote-firmware-")
    try:
        firmware_path = Path(tmpdir) / "firmware.bin"
        await loop.run_in_executor(None, firmware_path.write_bytes, firmware_bytes)

        cache_args = controller._build_cache_args(job)
        cmd = [
            *controller._esphome_cmd,
            "--dashboard",
            *cache_args,
            "upload",
            str(yaml_path),
            "--device",
            job.port,
            "--file",
            str(firmware_path),
        ]
        _LOGGER.debug("Remote upload subprocess: %s", " ".join(cmd))

        env = {**os.environ, **ESPHOME_SUBPROCESS_ENV}
        exit_code = await _run_upload_subprocess(
            controller=controller,
            job=job,
            bus=bus,
            cmd=cmd,
            env=env,
        )
    finally:
        await loop.run_in_executor(None, shutil.rmtree, tmpdir, True)

    if exit_code is None:
        # ``_run_upload_subprocess`` already finalised the
        # job (cancel during the subprocess run).
        return

    if exit_code == 0:
        _finalize_success(controller, job)
    else:
        _fail_locally(
            controller,
            job,
            error=f"remote build: local upload failed (exit {exit_code})",
        )


async def _run_upload_subprocess(
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    bus: EventBus,
    cmd: list[str],
    env: dict[str, str],
) -> int | None:
    """
    Spawn the local ``esphome upload`` and stream its output.

    Returns the subprocess exit code, or ``None`` when the
    runner already finalised the job locally (e.g. a Stop
    click landed and ``_finalize_cancelled`` ran). The caller
    treats ``None`` as "nothing more to do" and skips its own
    terminal-status mapping.

    Mirrors the local subprocess path's per-line bookkeeping
    (``_ingest_output_line``) so the firmware-tasks UI sees
    one event stream regardless of which CPU produced the
    bytes. ``_tracked_subprocess`` registers the spawn with
    ``controller._current_process`` so a concurrent
    ``firmware/cancel`` lands SIGTERM on the upload chain
    just like it does for the local-only path.
    """
    async with controller._tracked_subprocess(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        # Same process-group rationale as the local subprocess
        # path: SIGTERM has to walk the whole esphome →
        # platformio → esptool tree.
        start_new_session=True,
    ) as proc:
        # Honour a cancel that landed between the
        # ``download_artifacts`` await and the spawn — without
        # this check, the subprocess gets started for a job
        # the user already aborted.
        if job.job_id in controller._cancel_requested:
            await controller._terminate_current_process()

        assert proc.stdout is not None  # type narrowing
        async for line in iter_lines_with_progress(proc.stdout):
            _ingest_output_line(job, bus, line)

        exit_code = await proc.wait()

    if job.job_id in controller._cancel_requested:
        controller._finalize_cancelled(job)
        return None
    job.exit_code = exit_code
    return exit_code


def _finalize_success(controller: FirmwareController, job: FirmwareJob) -> None:
    """Mark *job* COMPLETED and fire ``JOB_COMPLETED`` on the local bus.

    Shared between every REMOTE success path:

    * The COMPILE-only branch on receiver-completed — there's
      no subprocess that produced an exit code, so callers
      stamp ``job.exit_code = 0`` before invoking this helper.
    * The UPLOAD / INSTALL branch after the local
      ``esphome upload`` subprocess returns ``0`` — the
      subprocess wrapper already stamped ``job.exit_code``
      with the real exit, so this helper just runs the
      finalize + fire pair.

    The legacy ``follow_job`` framing coerces a ``None``
    ``exit_code`` to a failure code (``1``); leaving the
    stamp on the caller forces the COMPILE path to make the
    "remote compile produced zero exit" choice explicit.
    """
    _mark_job_terminal(job, JobStatus.COMPLETED)
    payload: JobLifecycleData = {"job": job}
    controller.bus.fire(EventType.JOB_COMPLETED, payload)


async def _send_cancel_or_finalise(
    controller: FirmwareController,
    job: FirmwareJob,
) -> bool:
    """
    Translate a pending local cancel into a wire ``cancel_job``.

    Returns ``True`` if the cancel frame is on the wire and the
    caller should keep waiting for the receiver's cancelled
    terminal frame; ``False`` if the local side already
    finalised the job (because there's no live session to
    cancel against, or the controller was torn down). Splits
    out from :func:`_await_terminal`'s loop so the lookup-vs-
    send error paths each get their own ``except`` clause
    without nesting.
    """
    remote_build = controller._db.remote_build
    if remote_build is None:
        # Receiver controller torn down mid-run — finalise as
        # cancelled rather than spinning forever on a future
        # that nothing will set.
        controller._finalize_cancelled(job)
        return False
    try:
        client = remote_build._lookup_open_peer_link_client(
            job.source_pin_sha256, label="firmware_remote_cancel"
        )
        await client.cancel_job(job_id=job.job_id)
    except (CommandError, PeerLinkNoSessionError) as exc:
        # ``CommandError`` from the lookup means the receiver
        # is unpaired / mid-reconnect; ``PeerLinkNoSessionError``
        # from the send means the session dropped between the
        # lookup and the wire write. Both translate the user's
        # Stop click into a local CANCELLED finalise — without
        # this fallback the cancel sits forever waiting for a
        # confirmation frame that will never arrive.
        detail = exc.message if isinstance(exc, CommandError) else str(exc)
        _LOGGER.info(
            "remote cancel for job %s: peer-link unavailable (%s); finalising locally",
            job.job_id,
            detail,
        )
        controller._finalize_cancelled(job)
        return False
    return True


def _fail_locally(
    controller: FirmwareController,
    job: FirmwareJob,
    *,
    error: str,
) -> None:
    """Mark *job* FAILED with *error* and fire ``JOB_FAILED`` on the local bus.

    Centralises the "remote path can't proceed, finalise
    terminally" sequence so every early-exit failure branch
    above stays one line at the call site. The text rides
    into ``job.error`` so a frontend that already renders
    ``error`` for local failures shows the remote failure
    with no special-case code.

    Cancel intent wins: if the user already flipped
    ``_cancel_requested`` for this job (Stop click landed
    during bundle build / lookup / dispatch / session-lost
    detection — anywhere before the receiver could emit a
    terminal frame), finalise as CANCELLED instead. Mirrors
    the local subprocess path's contract — a Stop that
    happened to race a failure should not show up as a red
    error badge.
    """
    if job.job_id in controller._cancel_requested:
        controller._finalize_cancelled(job)
        _LOGGER.info("Remote job %s cancelled (failure path: %s)", job.job_id, error)
        return
    job.error = error
    _mark_job_terminal(job, JobStatus.FAILED)
    payload: JobLifecycleData = {"job": job}
    controller.bus.fire(EventType.JOB_FAILED, payload)
    _LOGGER.warning("Remote job %s failed: %s", job.job_id, error)
