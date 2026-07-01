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
from typing import TYPE_CHECKING, Any, Literal

from ...helpers.api import CommandError
from ...helpers.config_bundle import BundleBuildError, build_yaml_bundle
from ...helpers.remote_artifacts_materialise import (
    MaterialiseError,
    materialise_remote_artifacts,
)
from ...helpers.subprocess import iter_lines_with_progress
from ...models import (
    EventType,
    FirmwareJob,
    JobStatus,
    JobType,
    OffloaderJobOutputData,
    OffloaderJobStateChangedData,
    OffloaderPeerLinkClosedData,
)
from ..remote_build.peer_link_client import (
    DownloadArtifactsError,
    PeerLinkNoSessionError,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
)
from . import lifecycle
from .constants import ESPHOME_SUBPROCESS_ENV
from .helpers import _fire_job_progress, _ingest_output_line

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


class RemoteServerLostError(Exception):
    """The build server's peer-link session closed mid-build.

    Raised by :func:`run_remote_job` only when ``retry_on_server_loss``
    is set (the dispatch pool) so the job re-routes to another worker
    instead of failing. The message is the close reason text.
    """


async def run_remote_job(  # noqa: C901
    controller: FirmwareController,
    job: FirmwareJob,
    *,
    retry_on_server_loss: bool = False,
) -> None:
    """
    Run a REMOTE-source firmware job and finalise *job* on the offloader bus.

    Caller (``FirmwareController._execute_job``) has already
    set ``status = RUNNING`` and fired ``JOB_STARTED``; this
    function is responsible for the entire run-and-finalise
    middle and leaves the outer ``finally`` block to clear
    ``_current_job`` / persist.

    With ``retry_on_server_loss`` (the dispatch pool), a mid-build
    peer-link close raises :class:`RemoteServerLostError` instead of
    finalising FAILED, so the caller can re-route to another worker.

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
    * :attr:`JobType.CLEAN` — submit_job(target="clean"). The
      receiver re-extracts the bundle (uniform pipeline with
      compile / upload) then runs ``esphome clean <yaml>``,
      which wipes its ``<data_dir>/build/<device_name>/``.
      No post-completion artifact fetch (nothing built to
      flash); finalise as ``COMPLETED`` on receiver's
      terminal frame. Fan-out from the offloader's local
      ``firmware/clean`` queues one of these per connected
      peer so receivers that built this device locally drop
      their stale artifacts too.

    Other job types (``RENAME`` / ``RESET_BUILD_ENV``) are
    rejected at the top because the receiver-side
    ``submit_job`` contract doesn't carry a wire shape for
    them yet.
    """
    if job.job_type not in (JobType.COMPILE, JobType.UPLOAD, JobType.INSTALL, JobType.CLEAN):
        _fail_locally(
            controller,
            job,
            reason=(
                f"unsupported job_type {job.job_type.value!r} (COMPILE/UPLOAD/INSTALL/CLEAN only)"
            ),
        )
        return

    if not job.source_pin_sha256:
        _fail_locally(controller, job, reason="missing source_pin_sha256")
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
    controller.state.cancel_events[job.job_id] = cancel_event
    # A cancel that landed during ``_execute_job``'s pre-runner
    # phase (``_current_job = job`` is set before the persist
    # await, so the cancel handler accepts the request and
    # writes to ``_cancel_requested`` — but the runner hasn't
    # yet installed its event, so the handler's
    # ``_cancel_events.get(job_id)`` returns ``None`` and the
    # ``set()`` is skipped). Replay the late wake here so the
    # runner doesn't park on an event that will never fire.
    if job.job_id in controller.state.cancel_requested:
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
                    retry_on_server_loss=retry_on_server_loss,
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
        controller.state.cancel_events.pop(job.job_id, None)


async def _dispatch_and_drive(
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    terminal: asyncio.Future[OffloaderJobStateChangedData],
    session_lost: asyncio.Future[OffloaderPeerLinkClosedData],
    cancel_event: asyncio.Event,
    retry_on_server_loss: bool = False,
) -> None:
    """Build the bundle, submit, then wait for the receiver's terminal frame.

    Split out from :func:`run_remote_job` so the listener
    attach / detach lives in one ``with`` block at the outer
    call site — every early-return failure path here still
    releases the bus subscriptions.
    """
    bundle_bytes = await _build_bundle_or_fail(controller, job)
    if bundle_bytes is None:
        return
    client = _open_peer_link_client_or_fail(controller, job)
    if client is None:
        return
    if not await _submit_job_to_receiver(
        controller=controller, job=job, client=client, bundle_bytes=bundle_bytes
    ):
        return

    wire_status = await _await_terminal(
        controller=controller,
        job=job,
        terminal=terminal,
        session_lost=session_lost,
        cancel_event=cancel_event,
        retry_on_server_loss=retry_on_server_loss,
    )
    if wire_status != "completed":
        # ``_await_terminal`` already finalised the job.
        return

    await _finalise_after_receiver_completed(controller=controller, job=job, client=client)


async def _build_bundle_or_fail(controller: FirmwareController, job: FirmwareJob) -> bytes | None:
    """Build the YAML bundle; ``None`` + ``_fail_locally`` on failure."""
    loop = asyncio.get_running_loop()
    yaml_path = await loop.run_in_executor(
        None, controller._db.settings.rel_path, job.configuration
    )
    try:
        return await build_yaml_bundle(yaml_path)
    except FileNotFoundError:
        _fail_locally(controller, job, reason=f"configuration not found: {job.configuration}")
    except BundleBuildError as exc:
        _fail_locally(controller, job, reason=f"bundle failed: {exc.output or exc}")
    return None


def _open_peer_link_client_or_fail(
    controller: FirmwareController, job: FirmwareJob
) -> PeerLinkClient | None:
    """Look up the offloader's open peer-link client; ``None`` on failure."""
    offloader = controller._db.remote_build_offloader
    if offloader is None:
        _fail_locally(controller, job, reason="controller not initialised")
        return None
    try:
        return offloader._lookup_open_peer_link_client(
            job.source_pin_sha256, label="firmware_remote"
        )
    except CommandError as exc:
        _fail_locally(controller, job, reason=f"receiver not reachable: {exc.message}")
        return None


def _local_device_display_for_job(
    controller: FirmwareController, job: FirmwareJob
) -> tuple[str, str]:
    """Return ``(name, friendly_name)`` for *job*'s configuration; ``("", "")`` if unknown.

    The receiver renders these in its firmware-tasks UI; sending
    them on the wire avoids re-parsing the bundled YAML for a
    title. ``getattr`` falls through cleanly when the devices
    controller isn't wired yet.
    """
    devices_controller = getattr(controller._db, "devices", None)
    if devices_controller is None:
        return "", ""
    device = devices_controller.get_by_configuration(job.configuration)
    return (device.name, device.friendly_name) if device is not None else ("", "")


async def _submit_job_to_receiver(
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    client: PeerLinkClient,
    bundle_bytes: bytes,
) -> bool:
    """Send ``submit_job`` and return ``True`` on accepted ack, ``False`` otherwise."""
    device_name, device_friendly_name = _local_device_display_for_job(controller, job)
    # CLEAN goes as target="clean" so the receiver runs
    # ``esphome clean`` after extract; everything else stays on
    # "compile" — the receiver only ever compiles.
    wire_target: Literal["compile", "clean"] = (
        "clean" if job.job_type is JobType.CLEAN else "compile"
    )
    try:
        ack = await client.submit_job(
            job_id=job.job_id,
            configuration_filename=job.configuration,
            target=wire_target,
            bundle_bytes=bundle_bytes,
            device_name=device_name,
            device_friendly_name=device_friendly_name,
        )
    except (PeerLinkNoSessionError, SubmitJobTimeoutError, SubmitJobSessionLostError) as exc:
        _fail_locally(controller, job, reason=f"dispatch failed: {exc}")
        return False
    if not ack["accepted"]:
        reason = ack.get("reason", "no reason given")
        _fail_locally(controller, job, reason=f"receiver rejected job: {reason}")
        return False
    return True


async def _finalise_after_receiver_completed(
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    client: PeerLinkClient,
) -> None:
    """Wire the post-completed dispatch by job_type.

    CLEAN finalises immediately (wipe-only). COMPILE / UPLOAD /
    INSTALL all materialise first; UPLOAD / INSTALL then spawn
    the local flash subprocess.
    """
    if job.job_type is JobType.CLEAN:
        job.exit_code = 0
        _finalize_success(controller, job)
        return
    if not await _fetch_and_materialise(controller=controller, job=job, client=client):
        return
    if job.job_type is JobType.COMPILE:
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
    retry_on_server_loss: bool = False,
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
                # Pool dispatch re-routes to another worker rather than
                # failing — but a pending cancel still wins.
                if retry_on_server_loss and job.job_id not in controller.state.cancel_requested:
                    raise RemoteServerLostError(text)
                # Push a synthetic output line BEFORE firing
                # JOB_FAILED so the live log stream ends with a
                # clear explanation of what cut the build off,
                # not the half-rendered compile line the receiver
                # had streamed before the link dropped.
                # ``job.error`` carries the same text for the
                # error banner; the log line makes the cause
                # visible in the dialog's main scroll buffer
                # without requiring the user to read the (often
                # truncated) red error toast.
                #
                # ``reason`` covers more than just transport drops:
                # ``transport_error`` is a network cut, but the
                # receiver can also push ``server_shutting_down``
                # (graceful restart), ``superseded`` (a fresh
                # session displaced this one), ``pin_mismatch`` /
                # ``peer_revoked`` (security), and the offloader
                # itself can close with ``client_stopped``. Use
                # "session closed" as the umbrella wording and
                # let the embedded ``text`` carry the specific
                # cause, instead of falsely framing every close
                # as a connection loss.
                #
                # Leading-newline avoidance: only insert a
                # separator newline when the previous buffered
                # output line doesn't already end with one. The
                # receiver-side compile streams ``\n``-terminated
                # lines, so the common case skips the prefix and
                # the synthetic line lands flush against the
                # last compile output rather than adding a blank
                # line.
                prefix = "" if job.output and job.output[-1].endswith(("\n", "\r")) else "\n"
                _ingest_output_line(
                    job,
                    controller.bus,
                    f"{prefix}*** remote build session closed ({text}); "
                    "the build was aborted ***\n",
                )
                _fail_locally(
                    controller,
                    job,
                    reason=f"peer-link session lost ({text})",
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
    if lifecycle.cancel_if_requested(controller, job):
        # User cancel beat the receiver's terminal frame to the loop (receiver
        # completed / failed while our cancel was in flight). User intent wins,
        # finalise CANCELLED regardless of the status we received.
        return None
    status = data["status"]
    if status == "cancelled":
        controller._finalize_cancelled(job)
        return None
    if status == "failed":
        # Receiver-supplied error text rides into job.error; an
        # empty error_message (older receiver, internal bug)
        # falls back so subscribers always see a non-empty reason.
        _fail_locally(controller, job, reason=data["error_message"] or "compile failed")
        return None
    # ``completed`` — the only status the caller must act on.
    # Don't finalise here; the caller owes a local flash step
    # for UPLOAD / INSTALL.
    return status


async def _fetch_and_materialise(
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    client: PeerLinkClient,
) -> bool:
    """Download the receiver's tarball and materialise it on the offloader.

    Returns ``True`` when staging succeeded and the caller
    should continue, ``False`` when it already finalised the
    job (download / materialise failure, or a cancel raced in
    after the receiver completed).
    """
    _LOGGER.info(
        "Remote job %s: requesting build artefacts for configuration=%r from receiver",
        job.job_id,
        job.configuration,
    )
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
            reason=(f"download_artifacts failed: {exc} (check the build server logs for details)"),
        )
        return False

    try:
        await asyncio.get_running_loop().run_in_executor(
            None, materialise_remote_artifacts, packed.tarball, job.configuration
        )
    except MaterialiseError as exc:
        _fail_locally(controller, job, reason=f"materialise failed: {exc}")
        return False
    except OSError as exc:
        # Disk full / permission denied / transient IO. Catch
        # at this seam so the runner task doesn't crash; the
        # MaterialiseError branch covers the wire-shape failures.
        _fail_locally(controller, job, reason=f"materialise IO error: {exc}")
        return False

    # Honour a cancel that arrived between the receiver's completed frame and
    # us getting here; otherwise staging succeeded.
    return not lifecycle.cancel_if_requested(controller, job)


async def _fetch_and_run_local_upload(
    *,
    controller: FirmwareController,
    job: FirmwareJob,
    client: PeerLinkClient,
) -> None:
    """Flash the device locally after the receiver's COMPILE half is staged.

    Pre-condition: :func:`_fetch_and_materialise` has already
    pulled the tarball and staged it; the offloader's filesystem
    now looks as if a local compile produced the build, so
    ``esphome upload <yaml> --device <port>`` resolves through
    esphome's per-platform dispatch (no ``--file`` needed).
    """
    # Cancel that landed after ``_fetch_and_materialise``'s
    # own check returned True — same race window the old
    # one-shot helper covered.
    if lifecycle.cancel_if_requested(controller, job):
        return

    bus = controller.bus
    loop = asyncio.get_running_loop()
    yaml_path = await loop.run_in_executor(
        None, controller._db.settings.rel_path, job.configuration
    )

    # Reset the gauge at the compile → upload seam.
    # :func:`helpers._ingest_output_line` monotonically clamps:
    # any parsed percent that isn't strictly greater than
    # ``job.progress`` is dropped. The receiver-side compile
    # streams PIO / linker ``(N%)`` lines through the same
    # ingest and can push the gauge near 100; without this
    # reset every ``Uploading: [..] 5% / 10% / ...`` line the
    # local flash subprocess emits would fall below the
    # compile's high-water and the progress bar would appear
    # frozen at the compile peak for the entire upload phase.
    # Firing through :func:`_fire_job_progress` (no clamp)
    # rather than the ingest path keeps the reset explicit at
    # this phase boundary.
    _fire_job_progress(job, bus, 0)

    cache_args = controller._build_cache_args(job)
    cmd = [
        *controller.state.esphome_cmd,
        "--dashboard",
        *cache_args,
        "upload",
        str(yaml_path),
        "--device",
        job.port,
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
            reason=f"local upload failed (exit {exit_code})",
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
    bytes. ``_tracked_subprocess`` registers the spawn on the
    compile lane's ``current_process`` so a concurrent
    ``firmware/cancel`` lands SIGTERM on the upload chain
    just like it does for the local-only path.
    """
    # Register the local flash on the job's own lane (``lane_for``); a remote
    # install is an INSTALL job, so that resolves to the compile lane.
    lane = controller.state.lane_for(job)
    async with controller._tracked_subprocess(
        lane,
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
        if job.job_id in controller.state.cancel_requested:
            await controller._terminate_current_process(lane)

        assert proc.stdout is not None  # type narrowing
        async for line in iter_lines_with_progress(proc.stdout):
            _ingest_output_line(job, bus, line)

        exit_code = await proc.wait()

    if lifecycle.cancel_if_requested(controller, job):
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
    # Routes through the controller's terminal-finalise helper so
    # the mark + runner-slot-release + fire sequence stays in
    # lockstep with the local subprocess path in
    # :meth:`FirmwareController._execute_job` (see
    # :meth:`FirmwareController._finalize_terminal` for the
    # ``queue_status`` broadcaster ordering rationale).
    controller._finalize_terminal(job, JobStatus.COMPLETED)


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
    offloader = controller._db.remote_build_offloader
    if offloader is None:
        # Receiver controller torn down mid-run — finalise as
        # cancelled rather than spinning forever on a future
        # that nothing will set.
        controller._finalize_cancelled(job)
        return False
    try:
        client = offloader._lookup_open_peer_link_client(
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


_REMOTE_BUILD_ERROR_PREFIX = "remote build: "


def _fail_locally(
    controller: FirmwareController,
    job: FirmwareJob,
    *,
    reason: str,
) -> None:
    """Mark *job* FAILED with ``"remote build: {reason}"`` and fire ``JOB_FAILED`` locally.

    Cancel intent wins: a Stop that flipped
    ``_cancel_requested`` before the receiver's terminal frame
    finalises as CANCELLED instead, mirroring the local
    subprocess path's contract.
    """
    error = f"{_REMOTE_BUILD_ERROR_PREFIX}{reason}"
    if lifecycle.cancel_if_requested(controller, job):
        _LOGGER.info("Remote job %s cancelled (failure path: %s)", job.job_id, error)
        return
    controller._finalize_terminal(job, JobStatus.FAILED, error=error)
    _LOGGER.warning("Remote job %s failed: %s", job.job_id, error)
