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
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ...helpers.peer_link_bundle import (
    BUNDLE_CHUNK_SIZE_BYTES,
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
from .artifacts_tarball import PackedArtifacts, pack_build_artifacts

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
    """Drives the receiver side of a ``download_artifacts`` flow.

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
            _LOGGER.warning(
                "download_artifacts from %s: rejecting job %s as duplicate "
                "(another download is already streaming for this session)",
                session.dashboard_id,
                job_id,
            )
            await self._send_reject(session, job_id, _REASON_DUPLICATE_DOWNLOAD)
            return

        firmware_job = self._find_remote_job(session.dashboard_id, job_id)
        if firmware_job is None:
            _LOGGER.warning(
                "download_artifacts from %s: no matching firmware job for "
                "remote_job_id=%s (peer's submitted jobs: %s)",
                session.dashboard_id,
                job_id,
                [
                    j.remote_job_id
                    for j in self._firmware._jobs.values()
                    if j.remote_peer == session.dashboard_id
                ],
            )
            await self._send_reject(session, job_id, _REASON_UNKNOWN_JOB)
            return
        if firmware_job.status != JobStatus.COMPLETED:
            _LOGGER.warning(
                "download_artifacts from %s: job %s (configuration=%r) is "
                "%s, not COMPLETED — rejecting",
                session.dashboard_id,
                job_id,
                firmware_job.configuration,
                firmware_job.status,
            )
            await self._send_reject(session, job_id, _REASON_JOB_NOT_COMPLETED)
            return

        self._inflight[session.dashboard_id] = _InflightDownload(job_id=job_id)
        try:
            loop = asyncio.get_running_loop()
            try:
                packed = await loop.run_in_executor(
                    None, pack_build_artifacts, firmware_job.configuration
                )
            except FileNotFoundError as exc:
                # ``FileNotFoundError`` from
                # :func:`load_build_artifacts` carries the actual
                # path that couldn't be opened (StorageJSON
                # sidecar, ``firmware_bin_path`` value, or
                # ``idedata.json``). Surface that path in the log
                # at WARNING so operators can compare the read
                # path against where esphome actually wrote the
                # build artefacts — the original symptom was
                # "Install failed" on the offloader with no
                # actionable detail because this log lived at
                # DEBUG. Production trips this when the receiver
                # crashes mid-build (no sidecar written) or when
                # the configuration string the offloader-submitted
                # job carries doesn't match the storage path
                # esphome uses for the compile.
                _LOGGER.warning(
                    "download_artifacts from %s: build artefacts missing for "
                    "job %s (configuration=%r): %s",
                    session.dashboard_id,
                    job_id,
                    firmware_job.configuration,
                    exc,
                )
                await self._send_reject(session, job_id, _REASON_BUILD_DIR_MISSING)
                return
            except Exception:
                _LOGGER.exception(
                    "download_artifacts from %s: pack failed for job %s (configuration=%r)",
                    session.dashboard_id,
                    job_id,
                    firmware_job.configuration,
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
        self, session: PeerLinkSession, job_id: str, packed: PackedArtifacts
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
