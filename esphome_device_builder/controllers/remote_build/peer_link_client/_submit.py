"""Submit-job / cancel-job / download-artifacts flow helpers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal, cast

from ....helpers.peer_link_bundle import (
    BUNDLE_CHUNK_SIZE_BYTES,
    chunk_bundle,
    compute_bundle_sha256,
    encode_chunk,
)
from ....models import (
    CancelJobFrameData,
    DownloadArtifactsFrameData,
    ResetBuildEnvAckFrameData,
    ResetBuildEnvFrameData,
    SubmitJobAckFrameData,
    SubmitJobChunkFrameData,
    SubmitJobFrameData,
)
from .._client_models import (
    DownloadArtifactsResult,
    PeerLinkNoSessionError,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
    _DownloadArtifactsState,
)
from ..peer_link import PeerLinkChannel

if TYPE_CHECKING:
    from .client import PeerLinkClient


# 60s headroom for the receiver's worst-case bundle-finalise +
# extract + queue-acquire path on a constrained SoC, without
# pinning the offloader's submit handler forever if the wire
# goes silent.
_SUBMIT_JOB_ACK_TIMEOUT_SECONDS = 60.0


async def submit_job(
    client: PeerLinkClient,
    *,
    job_id: str,
    configuration_filename: str,
    target: Literal["compile", "upload", "clean"],
    bundle_bytes: bytes,
    device_name: str = "",
    device_friendly_name: str = "",
    target_esphome_version: str = "",
) -> SubmitJobAckFrameData:
    """
    Send a ``submit_job`` header + chunked bundle and await the receiver's ack.

    Same-``job_id`` reentry mid-flow raises :class:`PeerLinkNoSessionError`;
    the WS layer should generate a fresh id per submit. Callers must not
    retry on timeout / session-loss: the receiver may have queued the job
    already.
    """
    channel = _require_open_channel(client, label="submit_job")
    ack_fut = _register_ack_future(client._submit_job_acks, job_id, label="submit_job")
    try:
        await _send_submit_job_frames(
            client,
            channel,
            job_id=job_id,
            configuration_filename=configuration_filename,
            target=target,
            bundle_bytes=bundle_bytes,
            device_name=device_name,
            device_friendly_name=device_friendly_name,
            target_esphome_version=target_esphome_version,
        )
        return await _await_ack(client, ack_fut, job_id=job_id, label="submit_job")
    finally:
        client._submit_job_acks.pop(job_id, None)


async def reset_build_env(client: PeerLinkClient, *, job_id: str) -> ResetBuildEnvAckFrameData:
    """
    Send a ``reset_build_env`` frame and await the receiver's enqueue ack.

    *job_id* is the offloader-side mirror job's id; the receiver echoes it
    on the ack and every fan-out frame of its reset job.
    """
    channel = _require_open_channel(client, label="reset_build_env")
    ack_fut = _register_ack_future(client._reset_env_acks, job_id, label="reset_build_env")
    frame: ResetBuildEnvFrameData = {"type": "reset_build_env", "job_id": job_id}
    try:
        if not await channel.send_frame(cast(dict[str, Any], frame)):
            raise SubmitJobSessionLostError(
                f"reset_build_env: request send failed mid-flow to "
                f"{client._hostname}:{client._port}"
            )
        return await _await_ack(client, ack_fut, job_id=job_id, label="reset_build_env")
    finally:
        client._reset_env_acks.pop(job_id, None)


async def cancel_job(client: PeerLinkClient, *, job_id: str) -> bool:
    """
    Send a ``cancel_job`` frame for *job_id* over the live session.

    Fire-and-forget; returns ``True`` if the frame went on the wire,
    ``False`` on same-tick channel failure.
    """
    channel = _require_open_channel(client, label="cancel_job")
    frame: CancelJobFrameData = {"type": "cancel_job", "job_id": job_id}
    return await channel.send_frame(cast(dict[str, Any], frame))


async def download_artifacts(client: PeerLinkClient, *, job_id: str) -> DownloadArtifactsResult:
    """
    Fetch the build-artifact tarball for *job_id* from the paired receiver.

    Returns the tarball + receiver-resolved ``firmware.bin`` flash offset
    (taken from the ``artifacts_start`` header — the tarball itself only
    carries the bootloader / partition / ota_data offsets via
    ``idedata.json``).

    Same-``job_id`` reentry raises :class:`PeerLinkNoSessionError`.
    Receiver-reported failures surface as :class:`DownloadArtifactsError`;
    session loss mid-download as :class:`SubmitJobSessionLostError`.
    """
    channel = _require_open_channel(client, label="download_artifacts")
    if job_id in client._artifacts_downloads:
        msg = (
            f"download_artifacts: future already registered for job_id={job_id!r} "
            f"(duplicate download on the same session)"
        )
        raise PeerLinkNoSessionError(msg)
    result: asyncio.Future[DownloadArtifactsResult] = asyncio.get_running_loop().create_future()
    client._artifacts_downloads[job_id] = _DownloadArtifactsState(future=result)
    try:
        frame: DownloadArtifactsFrameData = {
            "type": "download_artifacts",
            "job_id": job_id,
        }
        if not await channel.send_frame(cast(dict[str, Any], frame)):
            raise SubmitJobSessionLostError(
                f"download_artifacts: request send failed mid-flow to "
                f"{client._hostname}:{client._port}"
            )
        return await result
    finally:
        client._artifacts_downloads.pop(job_id, None)


def _require_open_channel(client: PeerLinkClient, *, label: str) -> PeerLinkChannel:
    """Return the live :class:`PeerLinkChannel` or raise :class:`PeerLinkNoSessionError`."""
    channel = client._active_channel
    if channel is None:
        msg = f"{label}: no live peer-link session to {client._hostname}:{client._port}"
        raise PeerLinkNoSessionError(msg)
    return channel


def _register_ack_future[AckT](
    acks: dict[str, asyncio.Future[AckT]], job_id: str, *, label: str
) -> asyncio.Future[AckT]:
    """Allocate + register the per-``job_id`` ack future in *acks*, refusing duplicates."""
    if job_id in acks:
        msg = (
            f"{label}: ack future already registered for job_id={job_id!r} "
            f"(duplicate {label} on the same session)"
        )
        raise PeerLinkNoSessionError(msg)
    # Register BEFORE the request goes out so a same-tick ack from the
    # receive loop can't beat the registration into the map.
    ack_fut: asyncio.Future[AckT] = asyncio.get_running_loop().create_future()
    acks[job_id] = ack_fut
    return ack_fut


async def _send_submit_job_frames(
    client: PeerLinkClient,
    channel: PeerLinkChannel,
    *,
    job_id: str,
    configuration_filename: str,
    target: Literal["compile", "upload", "clean"],
    bundle_bytes: bytes,
    device_name: str = "",
    device_friendly_name: str = "",
    target_esphome_version: str = "",
) -> None:
    """
    Send the ``submit_job`` header and every chunk frame, in order.

    Raises :class:`SubmitJobSessionLostError` immediately on mid-flow send failure.
    """
    total_bytes = len(bundle_bytes)
    num_chunks = (total_bytes + BUNDLE_CHUNK_SIZE_BYTES - 1) // BUNDLE_CHUNK_SIZE_BYTES
    header: SubmitJobFrameData = {
        "type": "submit_job",
        "job_id": job_id,
        "configuration_filename": configuration_filename,
        "target": target,
        "total_bundle_bytes": total_bytes,
        "num_chunks": num_chunks,
        "bundle_sha256": compute_bundle_sha256(bundle_bytes),
        "device_name": device_name,
        "device_friendly_name": device_friendly_name,
        "target_esphome_version": target_esphome_version,
    }
    if not await channel.send_frame(cast(dict[str, Any], header)):
        raise SubmitJobSessionLostError(
            f"submit_job: header send failed mid-flow to {client._hostname}:{client._port}"
        )
    # Streamed via ``chunk_bundle``'s generator rather than
    # materialising the list — slicing produces a fresh ``bytes``
    # per chunk and holding all of them alive would roughly double
    # peak memory (up to BUNDLE_MAX_TOTAL_BYTES = 4 MiB).
    for chunk_index, raw, is_last in chunk_bundle(bundle_bytes):
        chunk_frame: SubmitJobChunkFrameData = {
            "type": "submit_job_chunk",
            "job_id": job_id,
            "chunk_index": chunk_index,
            "data_b64": encode_chunk(raw),
            "is_last": is_last,
        }
        if not await channel.send_frame(cast(dict[str, Any], chunk_frame)):
            raise SubmitJobSessionLostError(
                f"submit_job: chunk {chunk_index} send failed mid-flow to "
                f"{client._hostname}:{client._port}"
            )


async def _await_ack[AckT](
    client: PeerLinkClient,
    ack_fut: asyncio.Future[AckT],
    *,
    job_id: str,
    label: str,
) -> AckT:
    """Park on *ack_fut* with a bounded timeout; raise structured errors."""
    try:
        return await asyncio.wait_for(ack_fut, timeout=_SUBMIT_JOB_ACK_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise SubmitJobTimeoutError(
            f"{label}: no ack from {client._hostname}:{client._port} "
            f"after {_SUBMIT_JOB_ACK_TIMEOUT_SECONDS:.0f}s "
            f"(job_id={job_id!r})"
        ) from exc
