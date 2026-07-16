"""
Offloader-side ``submit_job`` / ``download_artifacts`` / ``cancel_job`` WS commands.

The offloader packs the YAML config (and its referenced
files) into a gzipped tarball via the ``esphome bundle`` CLI,
streams it to the receiver behind a paired peer-link, and
tracks the build's lifecycle / artifacts.

Bodies take :class:`OffloaderController` as the first arg;
the controller keeps the three ``@api_command``-decorated WS
methods plus the two ``_validate`` / ``_build`` helpers as
thin bound-method delegates so test call-sites and the WS
dispatch resolve unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...models import OTA_PORT, ErrorCode, FirmwareJob, JobBuildSource, JobType
from ._validators import (
    download_artifacts_error_to_command_error,
    validate_pin_sha256,
    validate_submit_job_target,
)
from .artifacts_tarball import UnpackArtifactsError, unpack_artifacts_response
from .peer_link_client import (
    DownloadArtifactsError,
    DuplicateRequestError,
    PeerLinkNoSessionError,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
)

if TYPE_CHECKING:
    from .offloader import OffloaderController


async def validate_submit_job_config(
    controller: OffloaderController, configuration: object
) -> tuple[str, Path]:
    """Validate the WS *configuration* arg, return ``(name, yaml_path)``.

    Path-traversal boundary via :meth:`DashboardSettings.rel_path`;
    executor hop because ``Path.resolve`` is a syscall. Returns
    the resolved path so the downstream bundle build doesn't
    redo the hop.
    """
    if not isinstance(configuration, str) or not configuration:
        msg = "configuration must be a non-empty string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    yaml_path = await run_in_executor(controller._db.settings.rel_path, configuration)
    return configuration, yaml_path


async def build_submit_job_bundle(
    controller: OffloaderController, configuration: str, yaml_path: Path
) -> bytes:
    """Build the bundle bytes for *yaml_path*.

    Wraps :func:`helpers.config_bundle.build_yaml_bundle`
    (spawns the ``esphome bundle`` CLI). Maps
    :class:`FileNotFoundError` → ``NOT_FOUND`` and
    :class:`BundleBuildError` → ``INVALID_ARGS``; anything
    else propagates to ``INTERNAL_ERROR``. *configuration*
    is the original wire-arg used in diagnostics.
    """
    from ...helpers.config_bundle import (  # noqa: PLC0415
        BundleBuildError,
        build_yaml_bundle,
    )

    try:
        return await build_yaml_bundle(yaml_path)
    except FileNotFoundError as exc:
        raise CommandError(
            ErrorCode.NOT_FOUND, f"submit_job: YAML not found: {configuration}"
        ) from exc
    except BundleBuildError as exc:
        raise CommandError(
            ErrorCode.INVALID_ARGS,
            f"submit_job: bundle build failed for {configuration}: {exc.output or exc}",
        ) from exc


async def submit_job(
    controller: OffloaderController,
    *,
    pin_sha256: str,
    configuration: str,
    target: str,
) -> dict[str, Any]:
    """Bundle *configuration* and dispatch a build to the receiver behind *pin_sha256*.

    ``target="compile"`` streams the gzipped tarball over the
    existing peer-link session and returns the receiver's
    ``submit_job_ack``. ``target="upload"`` queues a server-pinned
    INSTALL :class:`FirmwareJob` locally and never touches the
    wire. Live job lifecycle + output ride
    ``OFFLOADER_JOB_STATE_CHANGED`` / ``OFFLOADER_JOB_OUTPUT``
    events on the ``subscribe_events`` stream either way.

    Returns ``{"job_id": <our id>, "accepted": <bool>,
    "reason": <str>}`` (``reason`` only on rejection).
    """
    clean_pin = validate_pin_sha256(pin_sha256)
    clean_target = validate_submit_job_target(target)
    clean_config, yaml_path = await controller._validate_submit_job_config(configuration)
    if clean_target == "upload":
        return await _queue_upload_as_local_install(controller, clean_pin, clean_config)
    client = controller._lookup_open_peer_link_client(clean_pin, label="submit_job")
    bundle_bytes = await controller._build_submit_job_bundle(clean_config, yaml_path)
    job_id = uuid4().hex[:12]
    try:
        ack = await client.submit_job(
            job_id=job_id,
            configuration_filename=clean_config,
            target=clean_target,
            bundle_bytes=bundle_bytes,
        )
    except (PeerLinkNoSessionError, DuplicateRequestError) as exc:
        raise CommandError(ErrorCode.PRECONDITION_FAILED, str(exc)) from exc
    except (SubmitJobTimeoutError, SubmitJobSessionLostError) as exc:
        raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc
    result: dict[str, Any] = {
        "job_id": ack["job_id"],
        "accepted": ack["accepted"],
    }
    if "reason" in ack:
        result["reason"] = ack["reason"]
    return result


async def download_artifacts(
    controller: OffloaderController, *, pin_sha256: str, job_id: str
) -> dict[str, Any]:
    """Fetch the build's flash-artifact set for *job_id* from the paired receiver.

    Sends ``download_artifacts{job_id}`` over the live
    peer-link to *pin_sha256*, parks on the assembled-bytes
    future the receive loop fills via
    ``artifacts_start`` / ``_chunk`` / ``_end`` frames,
    unpacks the SHA-256-verified gzipped tarball, and
    rewrites ``idedata.extra.flash_images[].path`` from
    receiver-absolute paths to the bare basenames the
    frontend's install path looks up.

    Returns ``{job_id, idedata, images, total_bytes}`` —
    ``images`` is ``firmware.bin`` first, then
    ``idedata.extra.flash_images`` in declared order.
    """
    clean_pin = validate_pin_sha256(pin_sha256)
    if not isinstance(job_id, str) or not job_id:
        msg = "job_id must be a non-empty string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    client = controller._lookup_open_peer_link_client(clean_pin, label="download_artifacts")
    try:
        packed = await client.download_artifacts(job_id=job_id)
    except (PeerLinkNoSessionError, DuplicateRequestError) as exc:
        raise CommandError(ErrorCode.PRECONDITION_FAILED, str(exc)) from exc
    except SubmitJobSessionLostError as exc:
        raise CommandError(ErrorCode.UNAVAILABLE, str(exc)) from exc
    except DownloadArtifactsError as exc:
        raise download_artifacts_error_to_command_error(exc) from exc
    try:
        return await run_in_executor(unpack_artifacts_response, packed, job_id)
    except UnpackArtifactsError as exc:
        raise CommandError(ErrorCode.INVALID_ARGS, str(exc)) from exc


async def cancel_job(
    controller: OffloaderController, *, pin_sha256: str, job_id: str
) -> dict[str, bool]:
    """Cancel the job behind *job_id* — locally when it names a local FirmwareJob.

    A *job_id* matching a local job (a server-pinned INSTALL
    from ``target="upload"``) cancels through
    ``firmware.cancel`` and returns ``{"sent": True}`` without
    touching the wire or reading *pin_sha256* past validation.
    Otherwise: fire-and-forget ``cancel_job`` frame to the
    receiver behind *pin_sha256*; the receiver's resulting
    ``job_state_changed{cancelled}`` is the confirmation,
    surfaced via ``OFFLOADER_JOB_STATE_CHANGED``, and
    ``sent=false`` is a same-tick channel failure the caller
    should treat as an error.
    """
    clean_pin = validate_pin_sha256(pin_sha256)
    if not isinstance(job_id, str) or not job_id:
        msg = "job_id must be a non-empty string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    # A server-pinned INSTALL is a local FirmwareJob the receiver only
    # learns about at dispatch; a wire-only cancel would be a silent no-op
    # while it sits queued (and during the local flash). ``firmware.cancel``
    # covers every phase — queued, remote compile (the runner's registered
    # cancel event translates to a wire ``cancel_job``), local flash. An
    # already-terminal job is a no-op success, matching the wire path's
    # receiver-drops-unknown-frame semantics.
    firmware = controller._db.firmware
    if firmware is not None and (job := await firmware.get_job(job_id=job_id)) is not None:
        if not job.is_terminal:
            await firmware.cancel(job_id=job_id)
        return {"sent": True}
    client = controller._lookup_open_peer_link_client(clean_pin, label="cancel_job")
    try:
        sent = await client.cancel_job(job_id=job_id)
    except PeerLinkNoSessionError as exc:
        raise CommandError(ErrorCode.PRECONDITION_FAILED, str(exc)) from exc
    return {"sent": sent}


async def reset_peer_build_env(controller: OffloaderController, *, pin_sha256: str) -> FirmwareJob:
    """Enqueue a mirror job that resets the receiver's whole build environment.

    The receiver runs its full local reset (``esphome clean-all`` +
    every cached venv) as its own job tagged with the returned mirror
    job's id; progress and the terminal state ride the normal firmware
    job events. The receiver refuses ``busy`` while it has any active
    job, which surfaces here as the mirror job failing with a
    retry-when-idle message.
    """
    clean_pin = validate_pin_sha256(pin_sha256)
    pairing = controller.get_pairing(clean_pin)
    if pairing is None:
        msg = "no pairing matches pin_sha256"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    if not pairing.reset_build_env_supported:
        msg = "the receiver does not support remote build-environment reset (update it)"
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
    # Connectivity precheck so a dead link refuses here instead of
    # enqueuing a job doomed to fail its dispatch.
    controller._lookup_open_peer_link_client(clean_pin, label="reset_peer_build_env")
    firmware = controller._db.firmware
    if firmware is None:
        msg = "firmware controller not available"
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
    job = firmware._create_job(
        "",
        JobType.RESET_BUILD_ENV,
        build_source=JobBuildSource.for_server(
            pin_sha256=clean_pin,
            label=pairing.label,
            esphome_version=pairing.esphome_version,
        ),
    )
    # supersede=False: reset jobs share the empty configuration key; the
    # default supersede would cancel an unrelated reset targeting another
    # server (the firmware/clean fan-out precedent).
    return await firmware._enqueue(job, supersede=False)


async def _queue_upload_as_local_install(
    controller: OffloaderController, pin_sha256: str, configuration: str
) -> dict[str, Any]:
    """Queue ``target="upload"`` as a server-pinned INSTALL: receiver compiles, we flash."""
    pairing = controller.get_pairing(pin_sha256)
    if pairing is None:
        msg = "no pairing matches pin_sha256"
        raise CommandError(ErrorCode.NOT_FOUND, msg)
    # Connectivity precheck so a dead link refuses here instead of
    # enqueuing a job doomed to fail its dispatch.
    controller._lookup_open_peer_link_client(pin_sha256, label="submit_job")
    firmware = controller._db.firmware
    if firmware is None:
        msg = "firmware controller not available"
        raise CommandError(ErrorCode.PRECONDITION_FAILED, msg)
    # Deliberate: the fused INSTALL holds the compile lane through the whole
    # remote compile + local flash (only REMOTE_PENDING compiles pool), which
    # is what keeps cancel working in every phase. The install-chain follow-up
    # (enqueue_install_or_defer with a build_source override) reclaims the
    # lane via the dispatch pool.
    job = firmware._create_job(
        configuration,
        JobType.INSTALL,
        port=OTA_PORT,
        build_source=JobBuildSource.for_server(
            pin_sha256=pin_sha256,
            label=pairing.label,
            esphome_version=pairing.esphome_version,
        ),
    )
    await firmware._enqueue(job)
    return {"job_id": job.job_id, "accepted": True}
