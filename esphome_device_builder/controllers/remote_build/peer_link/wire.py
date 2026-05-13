"""Peer-link wire enums: TerminateReason + AppMessageType."""

from __future__ import annotations

from enum import StrEnum


class TerminateReason(StrEnum):
    """
    Wire ``reason`` value on a structured ``terminate`` close frame.

    Sent inside an :attr:`AppMessageType.TERMINATE` application
    frame so the offloader's reconnect logic can branch
    on the reason rather than guessing from the WS close code.

    * ``SUPERSEDED`` — a fresh peer-link connect from the same
      ``dashboard_id`` displaces this older session. Standard
      "restarted offloader" path.
    * ``HEARTBEAT_TIMEOUT`` — three pings in a row without a
      matching pong. The session loop closes itself; the wire
      frame may not actually reach the peer (TCP is presumed
      dead) but the WS close is still graceful from the
      receiver's side.
    * ``SERVER_SHUTTING_DOWN`` — the receiver controller is
      stopping. Sent to every active session before
      :meth:`ReceiverController.stop` returns.
    * ``MALFORMED_FRAME`` — a frame fails Noise decrypt /
      JSON parse / shape validation. Closes the session
      immediately; peer can reconnect after the next handshake.
    """

    SUPERSEDED = "superseded"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    SERVER_SHUTTING_DOWN = "server_shutting_down"
    MALFORMED_FRAME = "malformed_frame"


class AppMessageType(StrEnum):
    """
    Wire ``type`` discriminator on post-handshake application frames.

    Each frame is one JSON object, Noise-encrypted with
    ChaCha20-Poly1305 by the established session before going
    on the wire (one frame per WS message). Bundle bytes ride
    inside JSON as base64 (``submit_job_chunk``) so the dispatch
    seam stays on one parse branch.
    """

    PING = "ping"
    PONG = "pong"
    TERMINATE = "terminate"
    QUEUE_STATUS = "queue_status"
    # Bundle upload + job lifecycle. ``submit_job`` is the
    # offloader-initiated header; bundle bytes follow as ordered
    # ``submit_job_chunk`` frames, the last with ``is_last=True``.
    # Receiver replies with one ``submit_job_ack`` after
    # reassembly. Mid-build the receiver pushes
    # ``job_state_changed`` and ``job_output`` back.
    SUBMIT_JOB = "submit_job"
    SUBMIT_JOB_CHUNK = "submit_job_chunk"
    SUBMIT_JOB_ACK = "submit_job_ack"
    JOB_STATE_CHANGED = "job_state_changed"
    JOB_OUTPUT = "job_output"
    # Offloader → receiver cooperative cancel. Fire-and-forget;
    # the resulting ``job_state_changed`` with ``status="cancelled"``
    # is the confirmation.
    CANCEL_JOB = "cancel_job"
    # Offloader → receiver build-artifact fetch (#106). The
    # receiver streams a gzipped tarball back: ``artifacts_start``
    # carries ``total_bytes`` + ``num_chunks`` + ``artifacts_sha256``,
    # then N ``artifacts_chunk`` frames (b64 in JSON), then
    # ``artifacts_end`` (success or failure-with-reason). Single
    # stream so the offloader gets bootloader / partitions /
    # firmware / idedata.json atomically with one SHA-256.
    DOWNLOAD_ARTIFACTS = "download_artifacts"
    ARTIFACTS_START = "artifacts_start"
    ARTIFACTS_CHUNK = "artifacts_chunk"
    ARTIFACTS_END = "artifacts_end"
