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

    JSON-encoded plaintext is wrapped in a ChaCha20-Poly1305
    transport frame via the established Noise session (one frame
    per WS message) before going on the wire.

    Bundle bytes ride inside JSON frames as base64-encoded
    chunks (``submit_job_chunk``) rather than a parallel
    binary-only path. The 33 % b64 overhead doesn't matter on
    typical 5-50 KiB ESPHome bundles, and keeping every frame
    JSON-shaped lets the dispatch seam stay uniform (one parse
    branch, easier to trace). Profiling can motivate a binary
    variant later if multi-MB bundles become common.
    """

    PING = "ping"
    PONG = "pong"
    TERMINATE = "terminate"
    QUEUE_STATUS = "queue_status"
    # 5c-1: bundle upload + job lifecycle. ``submit_job`` is the
    # offloader-initiated header (job_id + configuration +
    # bundle metadata); the bundle bytes follow as one or more
    # ``submit_job_chunk`` frames in monotonic order, the last
    # carrying ``is_last=True``. The receiver replies with a
    # single ``submit_job_ack`` (``accepted: bool`` plus an
    # optional ``reason``) once the full bundle has reassembled.
    # Mid-build, the receiver pushes ``job_state_changed``
    # (lifecycle transitions) and ``job_output`` (per-line
    # stdout/stderr) back to the offloader. Wires into the
    # firmware queue + controller seams.
    SUBMIT_JOB = "submit_job"
    SUBMIT_JOB_CHUNK = "submit_job_chunk"
    SUBMIT_JOB_ACK = "submit_job_ack"
    JOB_STATE_CHANGED = "job_state_changed"
    JOB_OUTPUT = "job_output"
    # Offloader → receiver cooperative cancel. Carries the
    # offloader-local ``job_id`` from the original ``submit_job``
    # header; receiver resolves it to the matching
    # ``FirmwareJob`` via the :class:`JobFanout` correlation
    # cache and calls ``FirmwareController.cancel``. No ack
    # frame in the reverse direction — cancellation is fire-
    # and-forget; the next ``job_state_changed`` with
    # ``status="cancelled"`` is the confirmation the offloader
    # already has plumbing for.
    CANCEL_JOB = "cancel_job"
    # Offloader → receiver build-artifact fetch. The
    # offloader sends ``download_artifacts`` carrying the
    # offloader-supplied ``job_id`` from the original
    # ``submit_job`` header. The receiver resolves it to the
    # matching :class:`FirmwareJob` (must be in ``COMPLETED``
    # status — only completed builds have artifacts on disk),
    # packs the build directory's ``.pioenvs/<name>/*.bin`` /
    # ``*.uf2`` outputs plus ``idedata.json`` (esphome already
    # emits the latter — it carries the per-image flash
    # offsets the offloader's Web Serial / esptool path
    # needs) into a gzipped tar in an executor, then streams
    # the assembled bytes back as ``artifacts_start`` (header
    # with total_bytes + num_chunks + artifacts_sha256)
    # followed by ``artifacts_chunk`` frames (base64 inside
    # the JSON envelope, same shape as ``submit_job_chunk``)
    # followed by ``artifacts_end`` (success+sha256-confirmed
    # or failure-with-reason). Single stream rather than one
    # frame per artifact: the offloader gets bootloader.bin +
    # partitions.bin + firmware.bin + idedata.json in one
    # atomic transport with a single SHA-256, and the wire
    # format doesn't grow when a future platform adds another
    # required output. See issue #106.
    DOWNLOAD_ARTIFACTS = "download_artifacts"
    ARTIFACTS_START = "artifacts_start"
    ARTIFACTS_CHUNK = "artifacts_chunk"
    ARTIFACTS_END = "artifacts_end"
