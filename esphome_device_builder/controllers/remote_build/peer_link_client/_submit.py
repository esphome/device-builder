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
    ack_fut = _register_submit_job_ack_future(client, job_id)
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
        return await _await_submit_job_ack(client, ack_fut, job_id=job_id)
    finally:
        client._submit_job_acks.pop(job_id, None)


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


def _register_submit_job_ack_future(
    client: PeerLinkClient, job_id: str
) -> asyncio.Future[SubmitJobAckFrameData]:
    """Allocate + register the per-``job_id`` ack future, refusing duplicates."""
    if job_id in client._submit_job_acks:
        msg = (
            f"submit_job: ack future already registered for job_id={job_id!r} "
            f"(duplicate submit on the same session)"
        )
        raise PeerLinkNoSessionError(msg)
    # Register BEFORE the header goes out so a same-tick ack from the
    # receive loop can't beat the registration into the map.
    ack_fut: asyncio.Future[SubmitJobAckFrameData] = asyncio.get_running_loop().create_future()
    client._submit_job_acks[job_id] = ack_fut
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


async def _await_submit_job_ack(
    client: PeerLinkClient,
    ack_fut: asyncio.Future[SubmitJobAckFrameData],
    *,
    job_id: str,
) -> SubmitJobAckFrameData:
    """Park on *ack_fut* with a bounded timeout; raise structured errors."""
    try:
        return await asyncio.wait_for(ack_fut, timeout=_SUBMIT_JOB_ACK_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise SubmitJobTimeoutError(
            f"submit_job: no ack from {client._hostname}:{client._port} "
            f"after {_SUBMIT_JOB_ACK_TIMEOUT_SECONDS:.0f}s "
            f"(job_id={job_id!r})"
        ) from exc
