"""Peer-link wire-frame TypedDicts crossing the offloader/receiver boundary."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class QueueStatusFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.QUEUE_STATUS``.

    Sent by the receiver on every firmware-queue transition. The
    three fields aren't redundant — the ``running=False,
    queue_depth>0`` window exists between ``_queue.put(job)`` and
    the runner's ``_queue.get()``, so a scheduler reading only
    ``running`` would misclassify a fully-loaded receiver.
    """

    type: Literal["queue_status"]
    idle: bool
    running: bool
    queue_depth: int


class SubmitJobFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.SUBMIT_JOB``.

    Offloader-pushed header announcing a build before streaming
    the bundle. ``bundle_sha256`` (lowercase hex) lets the
    receiver verify the assembled stream. ``device_name`` /
    ``device_friendly_name`` are ``NotRequired`` so older
    offloaders that don't set them produce a valid frame;
    receiver-side title falls back to the last segment of the
    configuration path. ``target_esphome_version`` is the
    offloader's own esphome version; the receiver provisions a
    matching venv when it differs from its installed esphome
    (``NotRequired`` — older offloaders don't send it).
    """

    type: Literal["submit_job"]
    job_id: str
    configuration_filename: str
    target: Literal["compile", "upload", "clean"]
    total_bundle_bytes: int
    num_chunks: int
    bundle_sha256: str
    device_name: NotRequired[str]
    device_friendly_name: NotRequired[str]
    target_esphome_version: NotRequired[str]


class SubmitJobChunkFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.SUBMIT_JOB_CHUNK``.

    One slice of the bundle's gzipped tarball; bytes base64-
    encoded inside the JSON envelope. Chunks must arrive in
    monotonic order — the receiver's assembler rejects out-of-
    order / duplicate / post-completion frames so a misbehaving
    offloader gets ``terminate``'d cleanly.
    """

    type: Literal["submit_job_chunk"]
    job_id: str
    chunk_index: int
    data_b64: str
    is_last: bool


class SubmitJobAckFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.SUBMIT_JOB_ACK``.

    Receiver's response after the bundle stream completes.
    ``reason`` is ``NotRequired`` — present only on
    ``accepted=False`` carrying the structured error code; the
    success payload is ``{type, job_id, accepted: true}`` with
    no extra field. Missing ack inside the offloader's timeout
    tears the session down; **no** mid-session retry.
    """

    type: Literal["submit_job_ack"]
    job_id: str
    accepted: bool
    reason: NotRequired[str]


class JobStateChangedFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.JOB_STATE_CHANGED``.

    Receiver-pushed lifecycle transitions. Two distinct terminal
    fields, deliberately separate — one for humans, one for the
    dispatcher:

    * ``error_message`` — the HUMAN-readable one-line reason, empty
      on non-terminal states and on ``completed``, populated on
      ``failed`` / ``cancelled`` for the operator to read. Detailed
      build output flows separately through ``job_output``.
    * ``failure_reason`` — the MACHINE-readable category
      (:class:`JobFailureReason` value; ``NotRequired``, older
      receivers omit it → ``""``). ``"provision"`` means this
      ``failed`` terminal was because the receiver couldn't provision
      the esphome, so the offloader rebuilds locally; ``""`` / absent
      is an ordinary failure the offloader surfaces as-is.
    """

    type: Literal["job_state_changed"]
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    error_message: str
    failure_reason: NotRequired[str]


class JobOutputFrameData(TypedDict):
    r"""
    Application-frame payload for ``AppMessageType.JOB_OUTPUT``.

    Receiver-pushed line of build output. ``line`` is the raw
    stdout / stderr text **with its trailing terminator
    preserved** (``\n``, ``\r``, or ``\r\n``) — carriage-
    return-only chunks are esptool / PlatformIO progress
    overwrites that the offloader's ansi-log renderer leans on
    to decide append-vs-overwrite. Do not strip terminators at
    this layer.
    """

    type: Literal["job_output"]
    job_id: str
    stream: Literal["stdout", "stderr"]
    line: str


class DownloadArtifactsFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.DOWNLOAD_ARTIFACTS``.

    Offloader → receiver request to fetch the build-artifact
    tarball for a previously-completed remote build. ``job_id``
    is the offloader-supplied id from the original
    ``submit_job`` header (the value the receiver stashed as
    :attr:`FirmwareJob.remote_job_id`). The receiver responds
    with the artifacts_start / chunk / end stream on success,
    or an immediate ``artifacts_end{accepted=false}`` on
    failure (unknown correlation, non-terminal job, missing
    build dir, disk error).
    """

    type: Literal["download_artifacts"]
    job_id: str


class ArtifactsStartFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.ARTIFACTS_START``.

    Receiver-pushed header announcing the tarball stream. The
    ``artifacts_sha256`` hash is recomputed by the offloader
    after assembly as an integrity check for chunk-reordering
    bugs in our own framing — the per-frame Noise AEAD already
    covers wire-level confidentiality + authentication, so the
    hash isn't a security check.

    ``firmware_offset`` is the lowercase-hex flash offset for
    ``firmware.bin`` (e.g. ``"0x10000"`` on ESP32, ``"0x0"`` on
    ESP8266 / libretiny / RP2040), resolved receiver-side
    against ``StorageJSON.target_platform``. The remaining
    flash-image offsets ride inside ``idedata.json`` in the
    tarball.

    Fires only on the success path. A failed download sends
    ``artifacts_end`` with ``accepted=false`` directly.
    """

    type: Literal["artifacts_start"]
    job_id: str
    total_bytes: int
    num_chunks: int
    artifacts_sha256: str
    firmware_offset: str


class ArtifactsChunkFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.ARTIFACTS_CHUNK``.

    One slice of the build-artifact tarball; base64-encoded
    inside the JSON envelope. Chunks must arrive in monotonic
    order — the offloader's :class:`BundleAssembler` rejects
    out-of-order / duplicate / post-completion frames.
    """

    type: Literal["artifacts_chunk"]
    job_id: str
    chunk_index: int
    data_b64: str
    is_last: bool


class ArtifactsEndFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.ARTIFACTS_END``.

    Terminator frame for a ``download_artifacts`` request.
    ``accepted=true`` fires after the last chunk; the offloader
    validates the assembled bytes against ``artifacts_sha256``
    before resolving the download future. ``accepted=false``
    fires *instead of* any ``artifacts_start`` / ``artifacts_chunk``
    when the receiver refuses upfront (with a structured
    ``reason``). ``reason`` is ``NotRequired`` — omitted on accept.
    ``detail`` is an optional human-readable elaboration on a
    reject (e.g. the exact missing artefact path) the offloader
    appends to its error; ``NotRequired`` and only set on
    ``accepted=false``.
    """

    type: Literal["artifacts_end"]
    job_id: str
    accepted: bool
    reason: NotRequired[str]
    detail: NotRequired[str]


class CancelJobFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.CANCEL_JOB``.

    Offloader → receiver cooperative cancel for a previously-
    submitted job; fire-and-forget. The receiver's next
    ``job_state_changed{status: cancelled}`` is the
    confirmation. Cancel-of-already-terminal and
    cancel-of-unknown jobs are silently dropped at the
    receiver.
    """

    type: Literal["cancel_job"]
    job_id: str
