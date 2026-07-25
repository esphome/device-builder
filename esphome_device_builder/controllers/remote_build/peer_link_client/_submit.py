"""Submit-job / cancel-job / download-artifacts flow helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
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
    DuplicateRequestError,
    PeerLinkNoSessionError,
    SubmitJobSessionLostError,
    SubmitJobTimeoutError,
    _DownloadArtifactsState,
)
from ..peer_link import PeerLinkChannel

if TYPE_CHECKING:
    from .client import PeerLinkClient


# Flat floor for every ack: headroom for the receiver's queue-acquire
# path on a constrained SoC, without pinning the offloader's submit
# handler forever if the wire goes silent.
_SUBMIT_JOB_ACK_TIMEOUT_SECONDS = 60.0

# The receiver writes and extracts the whole bundle before acking a
# submit, so that ack's window grows with bundle size — sized for
# SD-card-backed SoCs sustaining only a few MiB/s (a max 128 MiB
# bundle adds ~64s on top of the floor).
_SUBMIT_ACK_BYTES_PER_SECOND = 2 * 1024 * 1024


def _submit_ack_timeout(bundle_size: int) -> float:
    """Return the submit-ack window: the flat floor plus size-proportional headroom."""
    return _SUBMIT_JOB_ACK_TIMEOUT_SECONDS + bundle_size / _SUBMIT_ACK_BYTES_PER_SECOND


async def submit_job(
    client: PeerLinkClient,
    *,
    job_id: str,
    configuration_filename: str,
    target: Literal["compile", "clean"],
    bundle_bytes: bytes,
    device_name: str = "",
    device_friendly_name: str = "",
    target_esphome_version: str = "",
) -> SubmitJobAckFrameData:
    """
    Send a ``submit_job`` header + chunked bundle and await the receiver's ack.

    Same-``job_id`` reentry mid-flow raises :class:`DuplicateRequestError`;
    the WS layer should generate a fresh id per submit. Callers must not
    retry on timeout / session-loss: the receiver may have queued the job
    already.
    """
    channel = _require_open_channel(client, label="submit_job")
    ack_fut = _register_pending(
        client._submit_job_acks,
        job_id,
        asyncio.get_running_loop().create_future,
        label="submit_job",
    )
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
        return await _await_ack(
            client,
            ack_fut,
            job_id=job_id,
            label="submit_job",
            timeout_seconds=_submit_ack_timeout(len(bundle_bytes)),
        )
    finally:
        client._submit_job_acks.pop(job_id, None)


async def reset_build_env(client: PeerLinkClient, *, job_id: str) -> ResetBuildEnvAckFrameData:
    """
    Send a ``reset_build_env`` frame and await the receiver's enqueue ack.

    *job_id* is the offloader-side mirror job's id; the receiver echoes it
    on the ack and every fan-out frame of its reset job.
    """
    channel = _require_open_channel(client, label="reset_build_env")
    ack_fut = _register_pending(
        client._reset_env_acks,
        job_id,
        asyncio.get_running_loop().create_future,
        label="reset_build_env",
    )
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

    Same-``job_id`` reentry raises :class:`DuplicateRequestError`.
    Receiver-reported failures surface as :class:`DownloadArtifactsError`;
    session loss mid-download as :class:`SubmitJobSessionLostError`.
    """
    channel = _require_open_channel(client, label="download_artifacts")
    state = _register_pending(
        client._artifacts_downloads,
        job_id,
        lambda: _DownloadArtifactsState(future=asyncio.get_running_loop().create_future()),
        label="download_artifacts",
    )
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
        return await state.future
    finally:
        client._artifacts_downloads.pop(job_id, None)


def _require_open_channel(client: PeerLinkClient, *, label: str) -> PeerLinkChannel:
    """Return the live :class:`PeerLinkChannel` or raise :class:`PeerLinkNoSessionError`."""
    channel = client._active_channel
    if channel is None:
        msg = f"{label}: no live peer-link session to {client._hostname}:{client._port}"
        raise PeerLinkNoSessionError(msg)
    return channel


def _register_pending[EntryT](
    pending: dict[str, EntryT], job_id: str, make_entry: Callable[[], EntryT], *, label: str
) -> EntryT:
    """Build + register the per-``job_id`` in-flight entry in *pending*, refusing duplicates."""
    if job_id in pending:
        msg = (
            f"{label}: request already registered for job_id={job_id!r} "
            f"(duplicate {label} on the same session)"
        )
        raise DuplicateRequestError(msg)
    # Register BEFORE the request goes out so a same-tick reply from the
    # receive loop can't beat the registration into the map.
    entry = pending[job_id] = make_entry()
    return entry


async def _send_submit_job_frames(
    client: PeerLinkClient,
    channel: PeerLinkChannel,
    *,
    job_id: str,
    configuration_filename: str,
    target: Literal["compile", "clean"],
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
    # peak memory (up to ``BUNDLE_MAX_TOTAL_BYTES``).
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
    timeout_seconds: float | None = None,
) -> AckT:
    """Park on *ack_fut* with a bounded timeout; raise structured errors."""
    timeout = timeout_seconds if timeout_seconds is not None else _SUBMIT_JOB_ACK_TIMEOUT_SECONDS
    try:
        return await asyncio.wait_for(ack_fut, timeout=timeout)
    except TimeoutError as exc:
        raise SubmitJobTimeoutError(
            f"{label}: no ack from {client._hostname}:{client._port} "
            f"after {timeout:.0f}s "
            f"(job_id={job_id!r})"
        ) from exc
