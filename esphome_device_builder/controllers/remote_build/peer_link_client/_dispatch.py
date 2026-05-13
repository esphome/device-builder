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


# Voluptuous schemas for the peer-supplied inbound wire frames
# the offloader receive loop dispatches into bus events / ack
# futures / download assemblers. Built via
# :func:`helpers.peer_link_frames.frame_schema` so the
# ``bool``-vs-``int`` special case (Python's
# ``isinstance(True, int) is True``) is handled the same way
# every shared frame schema in the project does. Optional
# fields (``SubmitJobAckFrameData.reason`` /
# ``ArtifactsEndFrameData.reason``) live outside the schema —
# the dispatch reads ``frame.get("reason")`` post-validate.
_SUBMIT_JOB_ACK_SCHEMA = frame_schema({"job_id": str, "accepted": bool})

_JOB_STATE_CHANGED_SCHEMA = frame_schema({"job_id": str, "status": str, "error_message": str})

_JOB_OUTPUT_SCHEMA = frame_schema({"job_id": str, "stream": str, "line": str})

_QUEUE_STATUS_SCHEMA = frame_schema({"idle": bool, "running": bool, "queue_depth": int})

# Schemas for the 6a artifact-download stream frames.
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

# Allowed ``status`` values on inbound ``job_state_changed``
# frames, mirroring :class:`JobStateChangedFrameData`'s
# ``Literal``. Membership check after the str-shape gate so a
# misbehaving receiver sending ``status="unknown"`` is dropped
# at the wire layer instead of fanning out a malformed bus
# event for downstream consumers.
_JOB_STATE_CHANGED_VALID_STATUS: frozenset[str] = frozenset(
    {"queued", "running", "completed", "failed", "cancelled"}
)

# Allowed ``stream`` values on inbound ``job_output`` frames,
# mirroring :class:`JobOutputFrameData`'s ``Literal``.
_JOB_OUTPUT_VALID_STREAM: frozenset[str] = frozenset({"stdout", "stderr"})


def log_malformed(client: PeerLinkClient, frame_type: str, parsed: dict[str, Any]) -> None:
    """Debug-log a frame that failed shape validation.

    Single call site for the per-dispatcher
    "malformed X frame from Y:Z" line so the format string
    doesn't drift across the four dispatchers.
    """
    _LOGGER.debug(
        "peer-link client malformed %s frame from %s:%d: %r",
        frame_type,
        client._hostname,
        client._port,
        parsed,
    )


def dispatch_queue_status(client: PeerLinkClient, parsed: dict[str, Any]) -> None:
    """Validate a ``queue_status`` frame and fire the offloader-side bus event.

    Drop silently on shape mismatch — the receiver will
    broadcast another snapshot on the next queue
    transition. The frame's ``queue_depth`` is ``int``;
    :func:`frame_schema` wraps every ``int`` field with
    :func:`not_bool` so a ``bool`` (which subclasses
    ``int``) doesn't slip through as a valid integer.
    """
    if not is_valid_frame(_QUEUE_STATUS_SCHEMA, parsed):
        log_malformed(client, "queue_status", parsed)
        return
    fire_queue_status(client, parsed["idle"], parsed["running"], parsed["queue_depth"])


def dispatch_submit_job_ack(client: PeerLinkClient, parsed: dict[str, Any]) -> None:
    """Resolve the matching ack future for an inbound ``submit_job_ack`` frame.

    Drops silently on:

    * Shape mismatch (missing / wrong-typed required fields)
      — the awaiter times out cleanly rather than seeing a
      malformed frame as a successful accept.
    * No matching future under *job_id* — the awaiter
      already raised :class:`SubmitJobTimeoutError` and
      popped its entry, or the receiver acked a job we
      didn't submit.
    * Future already done — duplicate ack under one
      *job_id*; the first wins and the second's
      ``set_result`` would raise ``InvalidStateError``.

    Optional ``reason`` (only present on rejection) is read
    post-validate and copied through.
    """
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
    # ``SubmitJobAckFrameData.reason`` is ``NotRequired`` and
    # carries the rejection code on ``accepted=False``. A
    # receiver that includes ``reason`` on accept is off-
    # contract — preserve the typed shape by dropping the
    # spurious field (logged at debug for the operator).
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
    """Validate + fan an inbound ``job_state_changed`` frame onto the bus.

    Same pattern as :func:`dispatch_queue_status`: validate
    first, drop silently on shape mismatch (a future
    retransmit will land cleanly), enrich with this
    client's receiver coordinates so subscribers can
    disambiguate transitions across multiple paired
    receivers.
    """
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
    """Validate + fan an inbound ``job_output`` frame onto the bus.

    High-rate path during an active build (one frame per
    line of compiler / linker output). Validate cheaply and
    drop on shape mismatch; subscribers see ``stream`` /
    ``line`` typed by :class:`OffloaderJobOutputData`.
    """
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
    """Validate ``artifacts_start`` + install the assembler for the in-flight download.

    Drops silently on shape mismatch / unknown job_id —
    the receive loop is hot and a malformed frame from a
    buggy peer shouldn't crash anyone. A stray
    ``artifacts_start`` for a job we never asked for
    means the awaiter already raised + popped its state
    (or it was a different session entirely); the safe
    thing is to ignore.
    """
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
    """Validate ``artifacts_chunk`` + feed the assembler.

    Out-of-order / oversized / decode-failure chunks
    from a buggy receiver resolve the future with
    :class:`DownloadArtifactsError`; the awaiter unwinds
    and the WS layer surfaces the structured reason.
    """
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
    """Validate ``artifacts_end`` + resolve the download future.

    Success path (``accepted=true``): finalise the
    assembler (validates count + SHA-256), set the
    future to the bytes. Failure path
    (``accepted=false``): pop ``reason`` and set the
    future to a :class:`DownloadArtifactsError` carrying
    it.
    """
    if not is_valid_frame(_ARTIFACTS_END_SCHEMA, parsed):
        log_malformed(client, "artifacts_end", parsed)
        return
    wire = cast(ArtifactsEndFrameData, parsed)
    state = client._artifacts_downloads.get(wire["job_id"])
    if state is None or state.future.done():
        return
    if not wire["accepted"]:
        reason = parsed.get("reason", "unknown")
        state.future.set_exception(
            DownloadArtifactsError(
                f"download_artifacts: receiver rejected ({reason})",
                reason=str(reason),
            )
        )
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
    """Fire ``OFFLOADER_PAIR_PIN_MISMATCH`` after a peer-link pin drift.

    Same event shape the pair-status listener already fires
    from :meth:`OffloaderController._apply_pair_status_result`
    on its own pin-drift branch. The controller listens for
    the event and stores the alert in
    ``_offloader_alerts`` so the snapshot path
    (``subscribe_events.initial_state.offloader_alerts``)
    carries it for late-subscribing tabs.

    ``expected_pin`` / ``observed_pin`` are the
    SHA-256 hashes of the pinned + observed pubkeys, in the
    same lowercase-hex form
    :class:`StoredPairing.pin_sha256` uses on disk.
    """
    payload: OffloaderPairPinMismatchData = {
        "receiver_hostname": client._hostname,
        "receiver_port": client._port,
        "receiver_label": client._receiver_label,
        "pin_sha256": client._pin_sha256,
        "expected_pin": pin_sha256_for_pubkey(client._pinned_static_x25519_pub),
        "observed_pin": pin_sha256_for_pubkey(observed),
    }
    client._bus.fire(EventType.OFFLOADER_PAIR_PIN_MISMATCH, payload)


def fire_queue_status(client: PeerLinkClient, idle: bool, running: bool, queue_depth: int) -> None:
    """Fire ``OFFLOADER_QUEUE_STATUS_CHANGED`` for an inbound snapshot.

    The peer-link receive loop validates the wire shape
    (boolean / int) before getting here, so the event
    payload's primitive contract holds without re-checking.
    Listeners on the bus include the
    :class:`OffloaderController`'s cache update and the
    ``subscribe_events`` re-broadcast.
    """
    payload: OffloaderQueueStatusChangedData = {
        "receiver_hostname": client._hostname,
        "receiver_port": client._port,
        "pin_sha256": client._pin_sha256,
        "idle": idle,
        "running": running,
        "queue_depth": queue_depth,
    }
    client._bus.fire(EventType.OFFLOADER_QUEUE_STATUS_CHANGED, payload)
