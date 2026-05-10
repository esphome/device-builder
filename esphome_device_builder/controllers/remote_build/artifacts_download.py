"""
Receiver-side ``download_artifacts`` flow for the remote-build peer-link.

Phase 6a of issue #106. Mirror of
:mod:`controllers.remote_build.submit_job`'s upload path but
running the other direction: the receiver, given an
offloader-supplied ``job_id`` for a previously-completed
``FirmwareJob``, packs the build's flash artifacts into a
gzipped tarball and streams the bytes back over the peer-link.

What goes in the tarball:

* **The flash images listed in ``idedata.extra.flash_images``** —
  plus ``firmware.bin`` itself, which upstream tracks
  separately on :attr:`StorageJSON.firmware_bin_path` rather
  than inside the ``extra`` block. Together this is the
  upstream-canonical "what to flash where" manifest esphome's
  own ``esptool`` install path consumes.
  For ESP32 the set is typically ``bootloader.bin`` +
  ``partitions.bin`` + ``ota_data_initial.bin`` +
  ``firmware.bin``; for ESP8266 just ``firmware.bin``; for
  libretiny / RP2040 the relevant single ``.bin`` (the
  ``.uf2`` path lives outside ``idedata`` upstream — followup
  if Mass Storage install lands).
* **``idedata.json``** itself — the offloader's frontend
  needs the per-image offsets to drive ``esptool`` /
  Web Serial. Shipping the file rather than reconstructing
  the manifest keeps the platform-variation matrix on the
  receiver side where it's already validated.

What is deliberately **excluded**:

* ``.elf`` / ``.map`` / ``.a`` / ``.o`` files (debug
  symbols + intermediate build outputs; multi-MB, never
  needed for flashing).
* Anything else in the build dir — ``compile_commands.json``,
  ``project_description.json``, ``platformio.ini``, the
  ``.piolibdeps`` tree. None of it is flashed; including it
  would bloat the transport and expose more receiver-side
  filesystem state than the offloader needs to install a
  build.

The packer reads the StorageJSON sidecar (the receiver's
canonical "this YAML's build output lives at <build_path> +
firmware bin at <firmware_bin_path>" record) plus the
cached ``idedata.json`` esphome writes after every compile.
Both are pure disk reads; no codegen, no ``CORE`` mutation,
no platformio reinvocation — the receiver already
authoritatively built the artifacts at submit-time, the
download path is just "stream them back."

Concurrency: single-flight per session — one in-flight
``download_artifacts`` per ``PeerLinkSession`` keyed on the
session's ``dashboard_id``. A second request from the same
session while the first is still streaming is rejected
with ``duplicate_download``. Different sessions (e.g. two
offloaders that both built the same device) each get
their own slot; the receiver's ``FirmwareJob`` map is
keyed on the offloader's ``dashboard_id`` so cross-session
collision is structurally impossible.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ...helpers.build_artifacts import load_build_artifacts
from ...helpers.peer_link_bundle import (
    BUNDLE_CHUNK_SIZE_BYTES,
    FIRMWARE_MAX_TOTAL_BYTES,
    chunk_bundle,
    compute_bundle_sha256,
    encode_chunk,
)
from ...helpers.peer_link_frames import frame_schema, is_valid_frame
from ...models import (
    ArtifactsChunkFrameData,
    ArtifactsEndFrameData,
    ArtifactsStartFrameData,
    DownloadArtifactsFrameData,
    JobStatus,
)

if TYPE_CHECKING:
    from ..firmware import FirmwareController
    from .peer_link import PeerLinkSession

_LOGGER = logging.getLogger(__name__)


# Reject reason codes carried on
# :class:`ArtifactsEndFrameData.reason` when ``accepted=False``.
# Same idiom as :data:`controllers.remote_build.submit_job._REASON_*` —
# the offloader-side submitter (6b) maps these to user-facing
# error messages. Only soft-rejects appear here; protocol
# violations (malformed frame shape) skip the ``artifacts_end``
# path entirely and terminate the session with
# ``MALFORMED_FRAME``.
_REASON_DUPLICATE_DOWNLOAD = "duplicate_download"
_REASON_UNKNOWN_JOB = "unknown_job"
_REASON_JOB_NOT_COMPLETED = "job_not_completed"
_REASON_BUILD_DIR_MISSING = "build_dir_missing"
_REASON_PACK_FAILED = "pack_failed"


# Required-field shape on the peer-controlled inbound frame.
# ``parse_app_frame`` confirms the JSON parses to a dict, but
# the inner shape is unchecked until this gate fires.
_DOWNLOAD_ARTIFACTS_SCHEMA = frame_schema({"job_id": str})


@dataclass
class _InflightDownload:
    """Per-session marker that a download is currently streaming.

    Just the ``job_id`` of the in-flight download — the
    duplicate-rejection check at ``handle_download_artifacts``
    looks at presence, not at the value. Stored on
    :attr:`ArtifactsDownloadSender._inflight` keyed on
    ``session.dashboard_id`` so a second concurrent request
    on the same session is rejected with
    ``duplicate_download`` rather than racing the assembler.
    """

    job_id: str


class ArtifactsDownloadSender:
    """Drives the receiver side of a ``download_artifacts`` flow (6a).

    One instance per :class:`RemoteBuildController` (created
    in :meth:`RemoteBuildController.start` alongside the
    :class:`SubmitJobReceiver`). Holds the in-flight
    download registry; the actual streaming work lives in
    :meth:`handle_download_artifacts` which the peer-link
    receive loop dispatches into.

    No persistent state beyond the in-flight registry —
    completed downloads drop their entry as soon as the
    final ``artifacts_end`` frame goes out. The build
    artifacts on disk are owned by the firmware controller
    + the per-peer-per-device build subtree convention
    from 5c-2a; 6c's TTL sweep is what reclaims those.
    """

    def __init__(self, firmware_controller: FirmwareController) -> None:
        self._firmware = firmware_controller
        # ``session.dashboard_id`` → in-flight download marker.
        # Populated at the start of a download, cleared in the
        # ``finally`` that ends the streaming work. The check
        # gates concurrent downloads on the same session;
        # different sessions each get their own slot because
        # the dispatch routes by ``dashboard_id``.
        self._inflight: dict[str, _InflightDownload] = {}

    def discard_session(self, dashboard_id: str) -> None:
        """Drop any in-flight download marker for *dashboard_id*.

        Called by the controller's session-teardown path
        (``unregister_peer_link_session``) so a half-finished
        download from a session that just dropped doesn't keep
        the slot occupied across reconnect.
        """
        self._inflight.pop(dashboard_id, None)

    async def handle_download_artifacts(
        self, session: PeerLinkSession, frame: dict[str, Any]
    ) -> None:
        """Validate, pack, and stream the build artifacts for *frame['job_id']*.

        Single-flight per session. Failure paths (malformed
        frame, unknown / non-completed job, missing build dir,
        pack failure) send a single ``artifacts_end`` with
        ``accepted=false`` and a structured ``reason``,
        without any preceding ``artifacts_start``. Success
        path sends ``artifacts_start`` → chunks →
        ``artifacts_end{accepted: true}``.

        Errors that imply wire-level peer misbehaviour
        (malformed frame shape) terminate the session with
        ``malformed_frame``; everything else lands as a
        soft reject the offloader can rerender as a clean
        user-facing message.
        """
        if not is_valid_frame(_DOWNLOAD_ARTIFACTS_SCHEMA, frame):
            _LOGGER.debug(
                "download_artifacts from %s: malformed frame; terminating: %r",
                session.dashboard_id,
                frame,
            )
            from .peer_link import TerminateReason  # noqa: PLC0415

            await session.terminate(TerminateReason.MALFORMED_FRAME)
            return

        typed = cast(DownloadArtifactsFrameData, frame)
        job_id = typed["job_id"]

        if session.dashboard_id in self._inflight:
            await self._send_reject(session, job_id, _REASON_DUPLICATE_DOWNLOAD)
            return

        firmware_job = self._find_remote_job(session.dashboard_id, job_id)
        if firmware_job is None:
            await self._send_reject(session, job_id, _REASON_UNKNOWN_JOB)
            return
        if firmware_job.status != JobStatus.COMPLETED:
            await self._send_reject(session, job_id, _REASON_JOB_NOT_COMPLETED)
            return

        self._inflight[session.dashboard_id] = _InflightDownload(job_id=job_id)
        try:
            loop = asyncio.get_running_loop()
            try:
                packed = await loop.run_in_executor(
                    None, _pack_build_artifacts, firmware_job.configuration
                )
            except FileNotFoundError:
                _LOGGER.debug(
                    "download_artifacts from %s: build dir / idedata missing for job %s",
                    session.dashboard_id,
                    job_id,
                    exc_info=True,
                )
                await self._send_reject(session, job_id, _REASON_BUILD_DIR_MISSING)
                return
            except Exception:
                _LOGGER.exception(
                    "download_artifacts from %s: pack failed for job %s",
                    session.dashboard_id,
                    job_id,
                )
                await self._send_reject(session, job_id, _REASON_PACK_FAILED)
                return
            await self._send_stream(session, job_id, packed)
        finally:
            self._inflight.pop(session.dashboard_id, None)

    def _find_remote_job(self, remote_peer: str, remote_job_id: str) -> Any:
        """Linear scan over ``FirmwareController._jobs`` for a matching remote job.

        Same shape as :meth:`JobFanout.resolve_firmware_job_id`
        but unconditional on terminal status — the download
        path needs to find COMPLETED jobs (which JobFanout
        evicts on terminal events). Walks ``_jobs`` directly;
        cardinality is bounded by the firmware queue's
        retention so the linear scan is cheap.

        Returns the :class:`FirmwareJob` or ``None`` on miss.
        """
        for job in self._firmware._jobs.values():
            if job.remote_peer == remote_peer and job.remote_job_id == remote_job_id:
                return job
        return None

    async def _send_reject(self, session: PeerLinkSession, job_id: str, reason: str) -> None:
        """Send a single ``artifacts_end{accepted: false, reason}`` and return."""
        end: ArtifactsEndFrameData = {
            "type": "artifacts_end",
            "job_id": job_id,
            "accepted": False,
            "reason": reason,
        }
        await session.send_app_frame(cast(dict[str, Any], end))

    async def _send_stream(
        self, session: PeerLinkSession, job_id: str, packed: _PackedArtifacts
    ) -> None:
        """Stream *packed*'s tarball as start → chunks → end{accepted: true}.

        Header carries ``total_bytes`` / ``num_chunks`` /
        ``artifacts_sha256`` / ``firmware_offset``; the
        offloader-side assembler validates each chunk against
        these and the resulting digest against the header
        hash before resolving the per-job download future.
        ``firmware_offset`` is the receiver-resolved
        flash-partition offset for ``firmware.bin`` — see
        :class:`models.remote_build.ArtifactsStartFrameData`
        for why the wire ships this rather than re-deriving
        on the offloader.
        """
        tarball = packed.tarball
        total_bytes = len(tarball)
        num_chunks = (total_bytes + BUNDLE_CHUNK_SIZE_BYTES - 1) // BUNDLE_CHUNK_SIZE_BYTES
        start: ArtifactsStartFrameData = {
            "type": "artifacts_start",
            "job_id": job_id,
            "total_bytes": total_bytes,
            "num_chunks": num_chunks,
            "artifacts_sha256": compute_bundle_sha256(tarball),
            "firmware_offset": packed.firmware_offset,
        }
        await session.send_app_frame(cast(dict[str, Any], start))
        for chunk_index, raw, is_last in chunk_bundle(tarball):
            chunk: ArtifactsChunkFrameData = {
                "type": "artifacts_chunk",
                "job_id": job_id,
                "chunk_index": chunk_index,
                "data_b64": encode_chunk(raw),
                "is_last": is_last,
            }
            await session.send_app_frame(cast(dict[str, Any], chunk))
        end: ArtifactsEndFrameData = {
            "type": "artifacts_end",
            "job_id": job_id,
            "accepted": True,
        }
        await session.send_app_frame(cast(dict[str, Any], end))


@dataclass(frozen=True)
class _PackedArtifacts:
    """Output of :func:`_pack_build_artifacts` — tarball bytes + start-frame fields.

    ``firmware_offset`` rides alongside the tarball so the
    sender can populate :attr:`ArtifactsStartFrameData.firmware_offset`
    without re-running
    :func:`helpers.build_artifacts.load_build_artifacts`.
    Same string form ``idedata.extra.flash_images`` uses
    (lowercase hex, ``0x`` prefix) — keeps the wire shape
    uniform across the firmware partition and the extras.
    """

    tarball: bytes
    firmware_offset: str


def _pack_build_artifacts(configuration: str) -> _PackedArtifacts:
    """Pack the build's flash artifacts for *configuration* into a gzipped tarball.

    Synchronous; meant to run inside an executor. Calls
    :func:`helpers.build_artifacts.load_build_artifacts` to
    discover the flash-image set + idedata bytes, then
    packs every image (flattened to its basename) + the
    ``idedata.json`` manifest into a single gzipped tarball.

    Returned tarball layout:

    .. code-block:: text

        idedata.json
        firmware.bin
        bootloader.bin       (ESP32 / native IDF only)
        partitions.bin       (ESP32 / native IDF only)
        ota_data_initial.bin (ESP32 / native IDF only)

    Files are flattened (no subdirectory structure) because
    the offloader-side install path only needs the bytes +
    the offsets from ``idedata.extra.flash_images`` (plus
    ``firmware.bin``'s offset, which the receiver puts on
    :attr:`ArtifactsStartFrameData.firmware_offset` because
    upstream tracks the firmware partition separately from
    the ``extra`` block). The flash-image entries in
    ``idedata.json`` reference each file by its absolute
    build-dir path on the receiver; the offloader's extractor
    rewrites those to the basenames it just unpacked.

    Raises :class:`FileNotFoundError` from
    :func:`load_build_artifacts` when the StorageJSON sidecar
    or any required artifact is missing. Raises
    :class:`RuntimeError` on duplicate-basename collision
    (defensive — upstream esphome doesn't emit this shape
    today, but we fail loudly rather than ship a silently-
    truncated set) or when the artifact set exceeds
    :data:`FIRMWARE_MAX_TOTAL_BYTES`. Two cap checks fire:
    the uncompressed walking sum (cheap, lets us short-circuit
    before reading huge files), and a final compressed-size
    check on the rendered tarball (the offloader's
    :class:`BundleAssembler` caps on
    ``ArtifactsStartFrameData.total_bytes``, the wire-side
    length, so the receiver-side ceiling needs to match).
    Compression usually shrinks the payload, so the
    uncompressed gate fires first in practice; the
    post-render check exists for the incompressible-data +
    tar-header-overhead corner where ``len(tarball)`` could
    technically exceed the uncompressed total. The caller
    (:meth:`ArtifactsDownloadSender.handle_download_artifacts`)
    catches both and surfaces a structured reject reason.
    """
    artifacts = load_build_artifacts(configuration)
    buf = io.BytesIO()
    total_uncompressed = len(artifacts.idedata_bytes)
    if total_uncompressed > FIRMWARE_MAX_TOTAL_BYTES:
        msg = (
            f"idedata.json for {configuration} ({total_uncompressed} bytes) "
            f"already exceeds FIRMWARE_MAX_TOTAL_BYTES {FIRMWARE_MAX_TOTAL_BYTES}"
        )
        raise RuntimeError(msg)
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # ``idedata.json`` first so the offloader can peek at
        # the manifest before chunking the binaries.
        info = tarfile.TarInfo(name="idedata.json")
        info.size = len(artifacts.idedata_bytes)
        tar.addfile(info, io.BytesIO(artifacts.idedata_bytes))

        seen_names: set[str] = set()
        for image in artifacts.flash_images:
            name = image.path.name
            if name in seen_names:
                msg = f"duplicate flash image basename {name!r} in idedata"
                raise RuntimeError(msg)
            seen_names.add(name)
            image_bytes = image.path.read_bytes()
            total_uncompressed += len(image_bytes)
            if total_uncompressed > FIRMWARE_MAX_TOTAL_BYTES:
                msg = (
                    f"build artifacts for {configuration} would exceed "
                    f"FIRMWARE_MAX_TOTAL_BYTES uncompressed "
                    f"({total_uncompressed} > {FIRMWARE_MAX_TOTAL_BYTES})"
                )
                raise RuntimeError(msg)
            info = tarfile.TarInfo(name=name)
            info.size = len(image_bytes)
            tar.addfile(info, io.BytesIO(image_bytes))

    # ``flash_images[0]`` is firmware.bin (load_build_artifacts
    # invariant) — its offset is the value the offloader needs
    # for the start frame, since idedata.json's manifest
    # doesn't include the firmware partition itself.
    tarball = buf.getvalue()
    # Final post-render cap on the wire-side length. The
    # uncompressed-walking gate above already short-circuits
    # the common case, but tar adds 512-byte headers per
    # member and gzip can grow incompressible data by a few
    # percent — for an artifact set that lands right at the
    # limit, ``len(tarball)`` could in principle exceed the
    # uncompressed total even though the body fits. The
    # offloader's ``BundleAssembler`` caps on
    # ``ArtifactsStartFrameData.total_bytes`` (the
    # post-render length), so without this match the receiver
    # could deterministically ship a stream the offloader
    # rejects.
    if len(tarball) > FIRMWARE_MAX_TOTAL_BYTES:
        msg = (
            f"build artifacts tarball for {configuration} would exceed "
            f"FIRMWARE_MAX_TOTAL_BYTES on the wire "
            f"({len(tarball)} > {FIRMWARE_MAX_TOTAL_BYTES})"
        )
        raise RuntimeError(msg)
    return _PackedArtifacts(tarball=tarball, firmware_offset=artifacts.flash_images[0].offset)
