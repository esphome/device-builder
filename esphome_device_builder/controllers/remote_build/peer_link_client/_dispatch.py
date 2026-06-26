"""Inbound peer-link frame dispatch + outbound event-firing helpers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from ....helpers.peer_link_bundle import (
    FIRMWARE_MAX_TOTAL_BYTES,
    BundleAssembler,
    BundleAssemblerError,
    decode_chunk,
)
from ....helpers.peer_link_frames import frame_schema, is_valid_frame
from ....helpers.peer_link_noise import pin_sha256_for_pubkey
from ....models import (
    ArtifactsChunkFrameData,
    ArtifactsEndFrameData,
    ArtifactsStartFrameData,
    EventType,
    JobOutputFrameData,
    JobStateChangedFrameData,
    OffloaderJobOutputData,
    OffloaderJobStateChangedData,
    OffloaderPairPeerRevokedData,
    OffloaderPairPinMismatchData,
    OffloaderPeerLinkClosedData,
    OffloaderPeerLinkOpenedData,
    OffloaderQueueStatusChangedData,
    SubmitJobAckFrameData,
)
from .._client_models import DownloadArtifactsError, DownloadArtifactsResult

if TYPE_CHECKING:
    from .client import PeerLinkClient

_LOGGER = logging.getLogger(__name__)


# Schemas built via ``frame_schema`` so the bool-vs-int special
# case (``isinstance(True, int) is True``) gets handled the same
# way it does for every shared frame schema in the project.
# Optional fields (``*FrameData.reason``) live outside the schema
# — dispatchers read via ``frame.get("reason")`` post-validate.
_SUBMIT_JOB_ACK_SCHEMA = frame_schema({"job_id": str, "accepted": bool})

_JOB_STATE_CHANGED_SCHEMA = frame_schema({"job_id": str, "status": str, "error_message": str})

_JOB_OUTPUT_SCHEMA = frame_schema({"job_id": str, "stream": str, "line": str})

_QUEUE_STATUS_SCHEMA = frame_schema({"idle": bool, "running": bool, "queue_depth": int})

_ARTIFACTS_START_SCHEMA = frame_schema(
    {
        "job_id": str,
        "total_bytes": int,
        "num_chunks": int,
        "artifacts_sha256": str,
        "firmware_offset": str,
    }
)

_ARTIFACTS_CHUNK_SCHEMA = frame_schema(
    {
        "job_id": str,
        "chunk_index": int,
        "data_b64": str,
        "is_last": bool,
    }
)

_ARTIFACTS_END_SCHEMA = frame_schema({"job_id": str, "accepted": bool})

# Mirror of the receiver-side reject-detail cap; re-applied here so a peer that
# ignores its own cap can't make our error string / job log unbounded (the
# session frame cap already bounds it, this is defense-in-depth).
_MAX_REJECT_DETAIL = 256

# Membership check on top of the str shape gate so a buggy
# receiver sending status="unknown" is dropped at the wire layer
# instead of fanning out a malformed bus event.
_JOB_STATE_CHANGED_VALID_STATUS: frozenset[str] = frozenset(
    {"queued", "running", "completed", "failed", "cancelled"}
)

# Same membership-check rationale for ``stream`` on inbound
# ``job_output`` frames.
_JOB_OUTPUT_VALID_STREAM: frozenset[str] = frozenset({"stdout", "stderr"})


def log_malformed(client: PeerLinkClient, frame_type: str, parsed: dict[str, Any]) -> None:
    """Debug-log a frame that failed shape validation."""
    _LOGGER.debug(
        "peer-link client malformed %s frame from %s:%d: %r",
        frame_type,
        client._hostname,
        client._port,
        parsed,
    )


def dispatch_queue_status(client: PeerLinkClient, parsed: dict[str, Any]) -> None:
    """Validate a ``queue_status`` frame and fire the offloader-side bus event."""
    if not is_valid_frame(_QUEUE_STATUS_SCHEMA, parsed):
        log_malformed(client, "queue_status", parsed)
        return
    fire_queue_status(
        client,
        idle=parsed["idle"],
        running=parsed["running"],
        queue_depth=parsed["queue_depth"],
    )


def dispatch_submit_job_ack(client: PeerLinkClient, parsed: dict[str, Any]) -> None:
    """Resolve the matching ack future for an inbound ``submit_job_ack`` frame."""
    if not is_valid_frame(_SUBMIT_JOB_ACK_SCHEMA, parsed):
        log_malformed(client, "submit_job_ack", parsed)
        return
    job_id = cast(str, parsed["job_id"])
    ack_fut = client._submit_job_acks.get(job_id)
    if ack_fut is None or ack_fut.done():
        _LOGGER.debug(
            "peer-link client dropping submit_job_ack from %s:%d "
            "(job_id=%r, has_future=%s, done=%s)",
            client._hostname,
            client._port,
            job_id,
            ack_fut is not None,
            ack_fut.done() if ack_fut is not None else False,
        )
        return
    accepted = cast(bool, parsed["accepted"])
    ack: SubmitJobAckFrameData = {
        "type": "submit_job_ack",
        "job_id": job_id,
        "accepted": accepted,
    }
    # Preserve the typed shape: ``reason`` is NotRequired and only
    # carries content on ``accepted=False``. Spurious ``reason`` on
    # accept is off-contract; drop it (logged at debug).
    reason = parsed.get("reason")
    if isinstance(reason, str):
        if accepted:
            _LOGGER.debug(
                "peer-link client dropping spurious reason=%r on accepted ack "
                "from %s:%d (job_id=%r)",
                reason,
                client._hostname,
                client._port,
                job_id,
            )
        else:
            ack["reason"] = reason
    ack_fut.set_result(ack)


def dispatch_job_state_changed(client: PeerLinkClient, parsed: dict[str, Any]) -> None:
    """Validate + fan an inbound ``job_state_changed`` frame onto the bus."""
    if not is_valid_frame(_JOB_STATE_CHANGED_SCHEMA, parsed):
        log_malformed(client, "job_state_changed", parsed)
        return
    if cast(str, parsed["status"]) not in _JOB_STATE_CHANGED_VALID_STATUS:
        log_malformed(client, "job_state_changed", parsed)
        return
    wire = cast(JobStateChangedFrameData, parsed)
    payload: OffloaderJobStateChangedData = {
        "receiver_hostname": client._hostname,
        "receiver_port": client._port,
        "pin_sha256": client._pin_sha256,
        "job_id": wire["job_id"],
        "status": wire["status"],
        "error_message": wire["error_message"],
    }
    client._bus.fire(EventType.OFFLOADER_JOB_STATE_CHANGED, payload)


def dispatch_job_output(client: PeerLinkClient, parsed: dict[str, Any]) -> None:
    """Validate + fan an inbound ``job_output`` frame onto the bus."""
    if not is_valid_frame(_JOB_OUTPUT_SCHEMA, parsed):
        log_malformed(client, "job_output", parsed)
        return
    if cast(str, parsed["stream"]) not in _JOB_OUTPUT_VALID_STREAM:
        log_malformed(client, "job_output", parsed)
        return
    wire = cast(JobOutputFrameData, parsed)
    payload: OffloaderJobOutputData = {
        "receiver_hostname": client._hostname,
        "receiver_port": client._port,
        "pin_sha256": client._pin_sha256,
        "job_id": wire["job_id"],
        "stream": wire["stream"],
        "line": wire["line"],
    }
    client._bus.fire(EventType.OFFLOADER_JOB_OUTPUT, payload)


def dispatch_artifacts_start(client: PeerLinkClient, parsed: dict[str, Any]) -> None:
    """Validate ``artifacts_start`` + install the assembler for the in-flight download."""
    if not is_valid_frame(_ARTIFACTS_START_SCHEMA, parsed):
        log_malformed(client, "artifacts_start", parsed)
        return
    wire = cast(ArtifactsStartFrameData, parsed)
    state = client._artifacts_downloads.get(wire["job_id"])
    if state is None:
        log_malformed(client, "artifacts_start", parsed)
        return
    try:
        state.assembler = BundleAssembler(
            total_bytes=wire["total_bytes"],
            num_chunks=wire["num_chunks"],
            sha256_hex=wire["artifacts_sha256"],
            max_total_bytes=FIRMWARE_MAX_TOTAL_BYTES,
        )
    except BundleAssemblerError as exc:
        if not state.future.done():
            state.future.set_exception(
                DownloadArtifactsError(
                    f"download_artifacts: invalid start header: {exc}",
                    reason="invalid_start_header",
                )
            )
        return
    state.firmware_offset = wire["firmware_offset"]


def dispatch_artifacts_chunk(client: PeerLinkClient, parsed: dict[str, Any]) -> None:
    """Validate ``artifacts_chunk`` + feed the assembler."""
    if not is_valid_frame(_ARTIFACTS_CHUNK_SCHEMA, parsed):
        log_malformed(client, "artifacts_chunk", parsed)
        return
    wire = cast(ArtifactsChunkFrameData, parsed)
    state = client._artifacts_downloads.get(wire["job_id"])
    if state is None or state.assembler is None:
        log_malformed(client, "artifacts_chunk", parsed)
        return
    try:
        raw = decode_chunk(wire["data_b64"])
        state.assembler.feed(wire["chunk_index"], raw, is_last=wire["is_last"])
    except BundleAssemblerError as exc:
        if not state.future.done():
            state.future.set_exception(
                DownloadArtifactsError(
                    f"download_artifacts: chunk failed: {exc}",
                    reason=exc.code.value,
                )
            )


def dispatch_artifacts_end(client: PeerLinkClient, parsed: dict[str, Any]) -> None:
    """Validate ``artifacts_end`` + resolve the download future (success or failure)."""
    if not is_valid_frame(_ARTIFACTS_END_SCHEMA, parsed):
        log_malformed(client, "artifacts_end", parsed)
        return
    wire = cast(ArtifactsEndFrameData, parsed)
    state = client._artifacts_downloads.get(wire["job_id"])
    if state is None or state.future.done():
        return
    if not wire["accepted"]:
        reason = parsed.get("reason", "unknown")
        # The receiver may include a ``detail`` (e.g. the exact missing artefact
        # path) so the operator-facing error names the real problem rather than
        # just the coarse reason code. ``reason`` still drives recovery policy.
        detail = parsed.get("detail")
        message = f"download_artifacts: receiver rejected ({reason})"
        if isinstance(detail, str) and detail:
            # Re-cap on this side too: don't trust the peer to have honoured the
            # sender's cap before echoing its detail into our error / job log.
            message = f"{message}: {detail[:_MAX_REJECT_DETAIL]}"
        state.future.set_exception(DownloadArtifactsError(message, reason=str(reason)))
        return
    if state.assembler is None:
        state.future.set_exception(
            DownloadArtifactsError(
                "download_artifacts: receiver acked success without sending artifacts_start",
                reason="missing_start",
            )
        )
        return
    try:
        tarball = state.assembler.finalise()
    except BundleAssemblerError as exc:
        state.future.set_exception(
            DownloadArtifactsError(
                f"download_artifacts: finalise failed: {exc}",
                reason=exc.code.value,
            )
        )
        return
    state.future.set_result(
        DownloadArtifactsResult(tarball=tarball, firmware_offset=state.firmware_offset)
    )


def fire_opened(client: PeerLinkClient, *, esphome_version: str = "") -> None:
    """Fire ``OFFLOADER_PEER_LINK_OPENED`` for a session that reached intent_response=ok."""
    payload: OffloaderPeerLinkOpenedData = {
        "receiver_hostname": client._hostname,
        "receiver_port": client._port,
        "pin_sha256": client._pin_sha256,
        "esphome_version": esphome_version,
    }
    client._bus.fire(EventType.OFFLOADER_PEER_LINK_OPENED, payload)


def fire_closed(client: PeerLinkClient, reason: str, *, error_detail: str = "") -> None:
    """Fire ``OFFLOADER_PEER_LINK_CLOSED`` for a session unwinding."""
    payload: OffloaderPeerLinkClosedData = {
        "receiver_hostname": client._hostname,
        "receiver_port": client._port,
        "pin_sha256": client._pin_sha256,
        "reason": reason,
        "error_detail": error_detail,
    }
    client._bus.fire(EventType.OFFLOADER_PEER_LINK_CLOSED, payload)


def fire_pin_mismatch(client: PeerLinkClient, *, observed: bytes) -> None:
    """Fire ``OFFLOADER_PAIR_PIN_MISMATCH`` after a peer-link pin drift."""
    payload: OffloaderPairPinMismatchData = {
        "receiver_hostname": client._hostname,
        "receiver_port": client._port,
        "receiver_label": client._receiver_label,
        "pin_sha256": client._pin_sha256,
        "expected_pin": pin_sha256_for_pubkey(client._pinned_static_x25519_pub),
        "observed_pin": pin_sha256_for_pubkey(observed),
    }
    client._bus.fire(EventType.OFFLOADER_PAIR_PIN_MISMATCH, payload)


def fire_peer_revoked(client: PeerLinkClient) -> None:
    """Fire ``OFFLOADER_PAIR_PEER_REVOKED`` after a terminal peer-link rejection."""
    payload: OffloaderPairPeerRevokedData = {
        "receiver_hostname": client._hostname,
        "receiver_port": client._port,
        "receiver_label": client._receiver_label,
        "pin_sha256": client._pin_sha256,
    }
    client._bus.fire(EventType.OFFLOADER_PAIR_PEER_REVOKED, payload)


def fire_queue_status(
    client: PeerLinkClient, *, idle: bool, running: bool, queue_depth: int
) -> None:
    """Fire ``OFFLOADER_QUEUE_STATUS_CHANGED`` for an inbound snapshot."""
    payload: OffloaderQueueStatusChangedData = {
        "receiver_hostname": client._hostname,
        "receiver_port": client._port,
        "pin_sha256": client._pin_sha256,
        "idle": idle,
        "running": running,
        "queue_depth": queue_depth,
    }
    client._bus.fire(EventType.OFFLOADER_QUEUE_STATUS_CHANGED, payload)
