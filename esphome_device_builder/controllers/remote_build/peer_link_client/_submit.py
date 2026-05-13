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


# How long :func:`submit_job` waits for the receiver's
# ``submit_job_ack`` after the last chunk goes out. Sized
# for the receiver's worst-case
# bundle-finalise + extract + queue-acquire path: SHA-256 over
# 4 MiB (capped at :data:`BUNDLE_MAX_TOTAL_BYTES`) is sub-100ms
# even on a Raspberry Pi class SoC, ``prepare_bundle_for_compile``
# walks the tar entries (a few hundred files, low-MiB), and the
# firmware queue's lock contention is bounded by the size of an
# individual ``_enqueue`` call. 60s gives generous headroom for
# a busy receiver under disk-IO contention without letting a
# silently-dead session pin the offloader's submit handler
# forever. Mismatch with no ack arriving inside the window
# raises :class:`SubmitJobTimeoutError` and the WS command
# surfaces a structured error to the caller.
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
) -> SubmitJobAckFrameData:
    """Send a ``submit_job`` header + chunked bundle and await the receiver's ack.

    Drives the offloader-side counterpart of the receiver's
    :class:`SubmitJobReceiver` accept path
    (:mod:`controllers.remote_build.submit_job`):

    1. Validate a session is live; raise
       :class:`PeerLinkNoSessionError` if not.
    2. Compute the bundle's SHA-256 + chunk count.
    3. Register a per-``job_id`` ack future on
       :attr:`_submit_job_acks` BEFORE the header goes out
       so a same-tick ack can't lose to the future
       registration (the receive loop runs on the same
       event loop; pre-registering avoids the race
       regardless).
    4. Send the header and stream every chunk through
       :meth:`PeerLinkChannel.send_frame`. A send failure
       (transport gone away mid-flow, JSON encode failure,
       Noise encrypt failure) raises
       :class:`SubmitJobSessionLostError` immediately
       rather than waiting for the timeout.
    5. Await the ack future with
       :data:`_SUBMIT_JOB_ACK_TIMEOUT_SECONDS`. Timeout
       raises :class:`SubmitJobTimeoutError`. Session loss
       during the wait raises
       :class:`SubmitJobSessionLostError` (the receive
       loop's ``finally`` propagates it via
       ``set_exception``).

    Concurrency: the WS dispatch is single-flight per
    connection, so the controller's WS handler invokes this
    sequentially per session. Multiple WS connections can
    invoke concurrently — distinct *job_id* values keep the
    ack futures separate, and :class:`PeerLinkChannel` holds
    the send lock that serialises wire encrypts. Same-
    ``job_id`` re-entry inside one session is rejected as
    :class:`PeerLinkNoSessionError` (a leftover ack future
    signals the previous flow hasn't completed); the WS
    layer should generate a fresh ``job_id`` per submit.

    No mid-session retry on timeout / session-loss: the
    receiver may have already accepted and queued the job,
    and a duplicate send under a fresh ``job_id`` would land
    a second :class:`FirmwareJob` on the receiver's queue.
    Operator-initiated retry on a fresh peer-link session
    is the correct recovery.
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
        )
        return await _await_submit_job_ack(client, ack_fut, job_id=job_id)
    finally:
        client._submit_job_acks.pop(job_id, None)


async def cancel_job(client: PeerLinkClient, *, job_id: str) -> bool:
    """Send a ``cancel_job`` frame for *job_id* over the live session.

    Fire-and-forget — the receiver's :class:`JobFanout`
    will fan out the resulting ``JOB_CANCELLED`` event as a
    ``job_state_changed{status: cancelled}`` frame, which
    the offloader's existing
    :attr:`OFFLOADER_JOB_STATE_CHANGED` listener handles.
    No per-call ack future, no timeout state on
    :class:`PeerLinkClient` — the next ``job_state_changed``
    on the inbound stream is the confirmation. A cancel-
    of-already-terminal or unknown job is silently dropped
    at the receiver (debug-logged); the offloader UI shows
    the most recent ``status`` regardless.

    Returns ``True`` if the frame made it onto the wire,
    ``False`` on a same-tick channel failure (Noise encrypt
    / WS send returned ``False``). Raises
    :class:`PeerLinkNoSessionError` when no live session
    exists; the WS layer maps that to
    ``CommandError(PRECONDITION_FAILED)``.
    """
    channel = _require_open_channel(client, label="cancel_job")
    frame: CancelJobFrameData = {"type": "cancel_job", "job_id": job_id}
    return await channel.send_frame(cast(dict[str, Any], frame))


async def download_artifacts(client: PeerLinkClient, *, job_id: str) -> DownloadArtifactsResult:
    """Fetch the build-artifact tarball for *job_id* from the paired receiver.

    Sends ``download_artifacts{job_id}``, parks on a per-
    job future the receive-loop dispatch fills as
    ``artifacts_start`` / ``artifacts_chunk`` /
    ``artifacts_end`` frames land. Returns a
    :class:`DownloadArtifactsResult` carrying the
    SHA-256-verified gzipped-tar bytes plus the
    receiver-resolved ``firmware.bin`` flash offset (taken
    from the ``artifacts_start`` header — the tarball
    itself doesn't carry the firmware partition's offset,
    only the ``extra`` flash-image entries do).

    Raises :class:`PeerLinkNoSessionError` if no live
    session exists; the WS layer maps that to
    ``CommandError(PRECONDITION_FAILED)``. Raises
    :class:`DownloadArtifactsError` (with structured
    ``reason``) on receiver-reported failure or
    offloader-side assembly mismatch. Raises
    :class:`SubmitJobSessionLostError` if the session
    ends mid-download (same drain shape as
    :func:`submit_job`).

    No timeout — artifact tarballs are 1-2 MiB typical
    (max :data:`FIRMWARE_MAX_TOTAL_BYTES` = 16 MiB);
    chunk stream completes within seconds on a LAN. If a
    bound becomes necessary it slots in as
    ``asyncio.wait_for`` around the future.

    Same-``job_id`` re-entry inside one session raises
    :class:`PeerLinkNoSessionError` (a leftover future
    signals the previous download hasn't completed); the
    WS layer should serialise downloads or generate a
    fresh request per page-load.
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
    """Return the live :class:`PeerLinkChannel` or raise :class:`PeerLinkNoSessionError`.

    ``label`` is folded into the exception message so each
    caller (``submit_job``, ``cancel_job``) names itself in
    the no-session log line. Every
    application-message sender that needs a live session
    flows through this single check; a future sender
    inherits the same exception class + WS-layer mapping
    without duplicating the channel-presence test.
    """
    channel = client._active_channel
    if channel is None:
        msg = f"{label}: no live peer-link session to {client._hostname}:{client._port}"
        raise PeerLinkNoSessionError(msg)
    return channel


def _register_submit_job_ack_future(
    client: PeerLinkClient, job_id: str
) -> asyncio.Future[SubmitJobAckFrameData]:
    """Allocate + register the per-``job_id`` ack future, refusing duplicates.

    The future is registered on :attr:`_submit_job_acks`
    BEFORE the header goes out so a same-tick ack can't
    lose to the future registration (the receive loop runs
    on the same event loop; pre-registering avoids the
    race regardless). A second call for the same *job_id*
    while the first is still pending raises
    :class:`PeerLinkNoSessionError` — same exception class
    the WS layer maps to "refuse the submit, ask the caller
    to retry under a fresh id."
    """
    if job_id in client._submit_job_acks:
        msg = (
            f"submit_job: ack future already registered for job_id={job_id!r} "
            f"(duplicate submit on the same session)"
        )
        raise PeerLinkNoSessionError(msg)
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
) -> None:
    """Send the ``submit_job`` header and every chunk frame, in order.

    Streams chunks via :func:`chunk_bundle`'s generator
    rather than materialising the list — slicing
    ``bundle_bytes`` produces a fresh ``bytes`` object per
    chunk, and holding them all alive at once would roughly
    double peak memory (up to :data:`BUNDLE_MAX_TOTAL_BYTES`,
    4 MiB). ``num_chunks`` is computed via integer ceil on
    ``total_bundle_bytes`` so the header still announces the
    exact count without a materialise step.

    Raises :class:`SubmitJobSessionLostError` immediately if
    any send returns ``False`` (transport gone away
    mid-flow, JSON encode failure, Noise encrypt failure)
    rather than ploughing on through the chunk loop and
    relying on the ack-await timeout to surface the failure.
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
    }
    if not await channel.send_frame(cast(dict[str, Any], header)):
        raise SubmitJobSessionLostError(
            f"submit_job: header send failed mid-flow to {client._hostname}:{client._port}"
        )
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
    """Park on *ack_fut* with a bounded timeout; raise structured errors.

    Timeout maps to :class:`SubmitJobTimeoutError`; session
    loss while parked surfaces as
    :class:`SubmitJobSessionLostError` (the receive loop's
    ``finally`` propagates it via ``set_exception``, which
    :meth:`asyncio.wait_for` re-raises).
    """
    try:
        return await asyncio.wait_for(ack_fut, timeout=_SUBMIT_JOB_ACK_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise SubmitJobTimeoutError(
            f"submit_job: no ack from {client._hostname}:{client._port} "
            f"after {_SUBMIT_JOB_ACK_TIMEOUT_SECONDS:.0f}s "
            f"(job_id={job_id!r})"
        ) from exc
