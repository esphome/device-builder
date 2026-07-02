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
from ...models import ErrorCode
from ._validators import (
    download_artifacts_error_to_command_error,
    validate_pin_sha256,
    validate_submit_job_target,
)
from .artifacts_tarball import UnpackArtifactsError, unpack_artifacts_response
from .peer_link_client import (
    DownloadArtifactsError,
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

    Streams the gzipped tarball over the existing peer-link
    session. Live job lifecycle + output ride
    ``OFFLOADER_JOB_STATE_CHANGED`` /
    ``OFFLOADER_JOB_OUTPUT`` events on the
    ``subscribe_events`` stream; this call returns only the
    receiver's ``submit_job_ack``.

    Returns ``{"job_id": <our id>, "accepted": <bool>,
    "reason": <str>}`` (``reason`` only on rejection).
    """
    clean_pin = validate_pin_sha256(pin_sha256)
    clean_target = validate_submit_job_target(target)
    clean_config, yaml_path = await controller._validate_submit_job_config(configuration)
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
    except PeerLinkNoSessionError as exc:
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
    except PeerLinkNoSessionError as exc:
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
    """Send a ``cancel_job`` frame to the receiver behind *pin_sha256*.

    Fire-and-forget cancel for a previously-submitted
    remote-driven job; the receiver's resulting
    ``job_state_changed{cancelled}`` is the confirmation,
    surfaced via ``OFFLOADER_JOB_STATE_CHANGED``.

    Returns ``{"sent": <bool>}`` reflecting whether the
    frame made it onto the wire; ``sent=false`` is a
    same-tick channel failure the caller should treat as
    an error.
    """
    clean_pin = validate_pin_sha256(pin_sha256)
    if not isinstance(job_id, str) or not job_id:
        msg = "job_id must be a non-empty string"
        raise CommandError(ErrorCode.INVALID_ARGS, msg)
    client = controller._lookup_open_peer_link_client(clean_pin, label="cancel_job")
    try:
        sent = await client.cancel_job(job_id=job_id)
    except PeerLinkNoSessionError as exc:
        raise CommandError(ErrorCode.PRECONDITION_FAILED, str(exc)) from exc
    return {"sent": sent}
