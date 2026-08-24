"""
Offloader-side ``download_artifacts`` / ``reset_peer_build_env`` WS commands.

Bodies take :class:`OffloaderController` as the first arg;
the controller keeps the ``@api_command``-decorated WS methods
as thin bound-method delegates so test call-sites and the WS
dispatch resolve unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...helpers.api import CommandError
from ...helpers.async_ import run_in_executor
from ...helpers.build_scheduler import build_source_for_pairing
from ...models import ErrorCode, FirmwareJob, JobType
from ._validators import (
    download_artifacts_error_to_command_error,
    validate_pin_sha256,
)
from .artifacts_tarball import UnpackArtifactsError, unpack_artifacts_response
from .peer_link_client import (
    DownloadArtifactsError,
    DuplicateRequestError,
    PeerLinkNoSessionError,
    SubmitJobSessionLostError,
)

if TYPE_CHECKING:
    from .offloader import OffloaderController


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
        build_source=build_source_for_pairing(
            pairing, controller.build_scheduler_snapshot().offloader_esphome_version
        ),
    )
    # supersede=False: reset jobs share the empty configuration key; the
    # default supersede would cancel an unrelated reset targeting another
    # server (the firmware/clean fan-out precedent).
    return await firmware._enqueue(job, supersede=False)
