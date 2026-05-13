"""Peer-link wire-frame TypedDicts crossing the offloader/receiver boundary."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class QueueStatusFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.QUEUE_STATUS``.

    Wire shape sent by the receiver-side
    :class:`ReceiverController` over an active peer-link
    session whenever the firmware queue transitions
    (``JOB_QUEUED`` / ``JOB_STARTED`` / terminal events).
    Encrypted under the established Noise session and
    serialised as JSON before going on the wire.

    The three fields aren't strictly redundant: the
    ``running=False, queue_depth>0`` window exists between
    ``await _queue.put(job)`` and the runner's ``_queue.get()``
    landing the same item, so a scheduler that reads only
    ``running`` would misclassify a fully-loaded receiver as
    accepting more work. ``idle`` and ``running`` carry both
    edges so the consumer can render any of "available",
    "busy", "queued" without re-deriving.
    """

    type: Literal["queue_status"]
    idle: bool
    running: bool
    queue_depth: int


# submit_job + bundle chunking + job lifecycle frames.
class SubmitJobFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.SUBMIT_JOB``.

    Header sent by the offloader to announce a build before
    streaming the bundle bytes. Carries the job's identity, the
    target configuration filename (relative to the bundle's
    extracted root, e.g. ``kitchen.yaml``), the build action
    (compile / upload), and the total bundle size + chunk
    count so the receiver can pre-size its assembler and reject
    a mismatched stream cleanly without unbounded buffering.

    ``bundle_sha256`` is the lowercase hex digest of the full
    bundle bytes; the receiver verifies the assembled stream
    against it before accepting the job. Cheap end-to-end
    integrity check on top of the per-frame Noise AEAD;
    catches a chunk-reassembly bug (e.g. a missed
    ``is_last``) that AEAD wouldn't surface.

    ``device_name`` / ``device_friendly_name`` carry the
    offloader's view of the device for the receiver-side
    firmware-tasks UI — the offloader already has both off
    its local Device list at install time, so the receiver
    avoids re-parsing the bundled YAML just to render a
    title. ``NotRequired`` so an older offloader that
    doesn't set them still produces a valid frame; the
    receiver-side title then falls back to the last segment
    of the configuration path. New offloaders always set
    both (empty string for ``device_friendly_name`` when the
    YAML doesn't define one).
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


class SubmitJobChunkFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.SUBMIT_JOB_CHUNK``.

    One slice of the bundle's gzipped tarball, carrying its
    ordinal index (``chunk_index``) and a flag marking the last
    chunk. Bytes are base64-encoded so the JSON envelope stays
    valid; the receiver decodes back to raw bytes before
    feeding the assembler. Chunks must arrive in monotonic
    order; the assembler rejects out-of-order, duplicate, or
    post-completion frames with a structured error so a
    misbehaving offloader can be ``terminate``'d cleanly
    instead of corrupting the on-disk extract.
    """

    type: Literal["submit_job_chunk"]
    job_id: str
    chunk_index: int
    data_b64: str
    is_last: bool


class SubmitJobAckFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.SUBMIT_JOB_ACK``.

    Receiver's response after the bundle stream completes (last
    chunk seen + ``bundle_sha256`` matches). ``accepted`` is
    ``False`` when the job can't be queued; bundle hash
    mismatch, manifest version unsupported, queue full, etc.
    ``reason`` carries the structured error code on rejection
    and is omitted on accept (``NotRequired`` so the wire
    payload is ``{type, job_id, accepted: true}`` on the
    success path with no extra field). The offloader treats a
    missing ack inside :data:`_SUBMIT_JOB_ACK_TIMEOUT_SECONDS`
    as a transport failure and tears the session down; it
    does **not** retry mid-session.
    """

    type: Literal["submit_job_ack"]
    job_id: str
    accepted: bool
    reason: NotRequired[str]


class JobStateChangedFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.JOB_STATE_CHANGED``.

    Receiver-pushed lifecycle transitions for a remote-driven
    job: ``queued`` (post-ack, before the runner picks it up),
    ``running`` (the runner has the slot), ``completed`` /
    ``failed`` / ``cancelled`` (terminal). One frame per
    transition; the firmware controller's existing JOB_*
    events drive the fan-out at the receiver-side wire layer.

    ``error_message`` is empty on non-terminal states and on
    ``completed``; populated on ``failed`` / ``cancelled``
    with a short human-readable string the offloader can
    surface to the user. Detailed output (compile errors,
    PlatformIO traces) flows separately through ``job_output``
    so the offloader's UI can render the streaming view
    without parsing the terminal frame.
    """

    type: Literal["job_state_changed"]
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    error_message: str


class JobOutputFrameData(TypedDict):
    r"""
    Application-frame payload for ``AppMessageType.JOB_OUTPUT``.

    Receiver-pushed line of build output. ``stream`` is
    ``stdout`` for the normal compile / upload trace and
    ``stderr`` for warnings / errors; the offloader can
    style them differently when surfacing to the UI without
    re-parsing.

    ``line`` is the raw stdout/stderr text *with its trailing
    terminator preserved* — ``\n``, ``\r``, or ``\r\n``. The
    terminator carries semantic info: carriage-return-only
    chunks are esptool / PlatformIO progress overwrites
    (the offloader's ansi-log renderer leans on the
    distinction to decide whether to append a new line or
    overwrite the last one). Stripping at this layer would
    lose that signal — the receiver-side
    :class:`JobOutputData` bus event preserves terminators
    for the same reason; the wire frame echoes that contract.

    Frames flow at high rate during an active build (one per
    line of compiler / linker output, easily 100+ frames per
    second on a cold compile); the channel's per-frame Noise
    AEAD overhead is the dominant cost. A future optimisation
    can batch consecutive lines into one frame, but 5c-1 keeps
    the wire shape one-line-per-frame for simplicity.
    """

    type: Literal["job_output"]
    job_id: str
    stream: Literal["stdout", "stderr"]
    line: str


class DownloadArtifactsFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.DOWNLOAD_ARTIFACTS``.

    Offloader → receiver request to fetch the build-artifact
    bundle for a previously-completed remote build. ``job_id``
    is the offloader-supplied id from the original
    ``submit_job`` header — the value the receiver stashed as
    :attr:`FirmwareJob.remote_job_id`. The receiver resolves
    it to the local :class:`FirmwareJob` by walking
    :attr:`FirmwareController._jobs`; the job must be in
    ``COMPLETED`` status (only completed builds have artifacts
    on disk).

    On success the receiver packs the build directory's
    ``.pioenvs/<name>/*.bin`` / ``*.uf2`` outputs plus
    ``idedata.json`` (esphome already emits the latter — it
    carries the per-image flash offsets the offloader's Web
    Serial / esptool path needs) into a gzipped tar, then
    streams back ``artifacts_start`` (header with total_bytes
    + num_chunks + artifacts_sha256) → N ``artifacts_chunk``
    frames → ``artifacts_end{accepted=true}``. On failure
    (unknown correlation, non-terminal job, missing build
    dir, disk read error) the receiver sends
    ``artifacts_end{accepted=false}`` and a structured
    ``reason`` immediately, without any preceding
    ``artifacts_start``.

    The assembled bytes on the offloader side are a tar.gz —
    extracting yields bootloader / partition / firmware
    binaries plus the idedata manifest in one atomic
    transport with a single SHA-256.
    """

    type: Literal["download_artifacts"]
    job_id: str


class ArtifactsStartFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.ARTIFACTS_START``.

    Receiver-pushed header announcing a build-artifact
    tarball stream for the offloader's previously-requested
    ``download_artifacts``. Carries ``total_bytes`` so the
    offloader can pre-size the
    assembly buffer + reject a mismatched stream cleanly;
    ``num_chunks`` matches the chunk count the receiver will
    actually send (assembler validates against this on every
    chunk); ``artifacts_sha256`` is the lowercase hex digest
    the offloader recomputes after assembly to catch
    chunk-reordering bugs in our own framing (the per-frame
    Noise AEAD already covers wire confidentiality +
    authentication, so the hash isn't a security check).

    ``firmware_offset`` is the lowercase-hex flash offset for
    the ``firmware.bin`` partition (e.g. ``"0x10000"`` on
    ESP32, ``"0x0"`` on ESP8266 / libretiny / RP2040). The
    receiver resolves this once via
    :func:`helpers.build_artifacts._firmware_offset_for_platform`
    against ``StorageJSON.target_platform`` — the offloader
    doesn't have access to that field over the wire and would
    otherwise need to duplicate the platform-detection logic
    upstream esphome already encapsulates. The remaining
    flash-image offsets (bootloader, partitions,
    ota_data_initial) ride inside ``idedata.json`` in the
    tarball, which is the upstream-canonical manifest for
    those entries.

    Fires only on the success path. A failed download sends
    ``artifacts_end`` with ``accepted=false`` and skips
    ``artifacts_start`` entirely.
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

    One slice of the build-artifact tarball. Same wire shape
    as :class:`SubmitJobChunkFrameData` but for
    the reverse direction — bytes are base64-encoded inside
    the JSON envelope so the dispatch seam stays uniform
    across the bundle-upload and artifacts-download flows.
    The offloader decodes back to raw bytes before feeding
    its :class:`BundleAssembler` (configured with
    :data:`FIRMWARE_MAX_TOTAL_BYTES`). Chunks must arrive in
    monotonic order; the assembler rejects out-of-order,
    duplicate, or post-completion frames with a structured
    error so a misbehaving receiver can be ``terminate``'d
    cleanly instead of corrupting the assembled bytes.
    """

    type: Literal["artifacts_chunk"]
    job_id: str
    chunk_index: int
    data_b64: str
    is_last: bool


class ArtifactsEndFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.ARTIFACTS_END``.

    Receiver's terminator frame for a ``download_artifacts``
    request. Doubles as the success/failure ack:

    * **Success path** — fires after the last chunk
      (``is_last=true``) has been sent; ``accepted=true``,
      ``reason`` omitted. The offloader validates the
      assembled bytes against the announced
      ``artifacts_sha256`` from ``artifacts_start`` before
      resolving the per-job download future with the
      tarball bytes.
    * **Failure path** — fires *instead of* any
      ``artifacts_start`` / ``artifacts_chunk`` when the
      receiver-side dispatch refuses the request upfront
      (unknown correlation, non-terminal job, missing build
      dir, pack failure, disk error). ``accepted=false``
      with a structured ``reason``; ``reason`` is omitted
      on accept (``NotRequired`` so the success payload is
      ``{type, job_id, accepted: true}`` with no extra
      field).
    """

    type: Literal["artifacts_end"]
    job_id: str
    accepted: bool
    reason: NotRequired[str]


class CancelJobFrameData(TypedDict):
    """
    Application-frame payload for ``AppMessageType.CANCEL_JOB``.

    Offloader → receiver cooperative cancel for a previously-
    submitted job. ``job_id`` is the offloader-supplied id from
    the original ``submit_job``
    header — i.e. the value the offloader generated and the
    receiver stashed as :attr:`FirmwareJob.remote_job_id`. The
    receiver resolves the offloader-side id back to its local
    :class:`FirmwareJob` via the :class:`JobFanout` correlation
    cache (keyed on ``(remote_peer=session.dashboard_id,
    remote_job_id)``) and routes the cancel through the
    firmware queue's existing :meth:`FirmwareController.cancel`
    primitive.

    No ack frame in the reverse direction: cancellation is
    fire-and-forget. The receiver's next ``job_state_changed``
    with ``status="cancelled"`` is the confirmation the
    offloader already plumbs through
    :attr:`EventType.OFFLOADER_JOB_STATE_CHANGED`. A
    cancel-of-already-terminal job raises
    :class:`CommandError(INVALID_ARGS)` inside
    :meth:`FirmwareController.cancel` which the handler
    swallows + debug-logs — the receiver was about to (or
    already has) emitted the natural terminal event and no
    further wire activity is needed. A cancel-of-unknown-job
    is debug-logged at the receiver and dropped (typically a
    race between offloader send and receiver-side terminal
    transition that already evicted the
    :class:`JobFanout` correlation entry).
    """

    type: Literal["cancel_job"]
    job_id: str
