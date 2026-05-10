"""
Receiver-side ``submit_job`` flow for the remote-build peer-link.

Phase 5c-2 of issue #106. Drives the post-handshake ``submit_job``
header + ``submit_job_chunk`` stream from the peer-link receive
loop into a queued :class:`FirmwareJob` carrying the offloader's
``dashboard_id`` in :attr:`FirmwareJob.remote_peer`. The
fan-out the other direction — pushing
``job_state_changed`` / ``job_output`` frames over the
submitting session — lands in the 5c-2b follow-up; this module
ends at "ack the bundle and queue the job."

Flow:

1. Offloader sends a ``submit_job`` header
   (``job_id`` / ``configuration_filename`` / ``target`` /
   ``total_bundle_bytes`` / ``num_chunks`` / ``bundle_sha256``).
   The receive loop forwards it to
   :meth:`SubmitJobReceiver.handle_submit_job`.
2. We construct a :class:`BundleAssembler` against the announced
   sizes / digest and store it in ``_inflight`` keyed on the
   session's ``dashboard_id``. One concurrent submit per session.
3. Offloader streams ``submit_job_chunk`` frames; the receive
   loop forwards each to
   :meth:`SubmitJobReceiver.handle_submit_job_chunk`. We
   base64-decode and feed the assembler. On the chunk that
   carries ``is_last=True`` we finalise (validates byte count
   + sha256), write the assembled tarball to
   ``<config>/.esphome/.remote_builds/<dashboard_id>/<device_name>/bundle.tar.gz``,
   extract via :func:`esphome.bundle.prepare_bundle_for_compile`
   (which preserves ``.esphome/`` / ``.pioenvs/`` for incremental
   builds — the load-bearing reason for the stable per-peer
   per-device subtree), and queue a :class:`FirmwareJob` with
   ``remote_peer=session.dashboard_id``.
4. We send a typed :class:`SubmitJobAckFrameData` — accepted on
   success, accepted=False with a structured ``reason`` on any
   of the rejection paths. Bundle-assembler errors that signal
   wire-level misbehaviour
   (:class:`BundleAssemblerError` outside the fix-with-retry set)
   also trigger ``terminate{reason: malformed_frame}`` because
   the offloader has already wandered off the wire format and
   continuing the session would only invite more corruption.

Per-peer per-device subtree: ``<dashboard_id>/<device_name>``.
The two-segment key dedupes correctly across multi-offloader
fleets (two HA Greens both shipping a "kitchen" device land in
distinct subtrees) without colliding within one offloader's
pool. PlatformIO's incremental-compile cache then sees stable
source paths between submissions and skips the cold-rebuild
hit. Phase-6 24h TTL sweeps cold subtrees later.
"""

from __future__ import annotations

import asyncio
import binascii
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from esphome.bundle import EsphomeError, prepare_bundle_for_compile

from ..helpers.peer_link_bundle import (
    BundleAssembler,
    BundleAssemblerError,
    BundleAssemblerErrorCode,
    decode_chunk,
)
from ..models import (
    JobType,
    SubmitJobAckFrameData,
    SubmitJobChunkFrameData,
    SubmitJobFrameData,
)

if TYPE_CHECKING:
    from .firmware import FirmwareController
    from .remote_build_peer_link import PeerLinkSession

_LOGGER = logging.getLogger(__name__)

# Reject reason codes carried on
# :class:`SubmitJobAckFrameData.reason` when ``accepted=False``.
# Distinct from :class:`BundleAssemblerErrorCode` (wire-level
# bundle problems): these cover the receiver-side dispatch path
# where the bundle assembled cleanly but something else went
# wrong (path traversal, extraction failure, queue rejection).
# The offloader's submitter (5c-3) maps these to user-facing
# error messages.
_REASON_DUPLICATE_SUBMIT = "duplicate_submit"
_REASON_INVALID_HEADER = "invalid_header"
_REASON_NO_INFLIGHT = "no_inflight_submit"
_REASON_JOB_ID_MISMATCH = "job_id_mismatch"
_REASON_CHUNK_DECODE_FAILED = "chunk_decode_failed"
_REASON_EXTRACT_FAILED = "extract_failed"
_REASON_QUEUE_REJECTED = "queue_rejected"

# Subdirectory under ``<config_dir>/.esphome/`` where remote-peer
# bundles land. Hidden by the leading dot so a casual ``ls`` of
# the user's main config tree doesn't show it next to their own
# YAMLs; living under ``.esphome/`` keeps it adjacent to other
# build artefacts (StorageJSON, build dirs) so phase-6's TTL
# sweep can reuse the same parent walk.
_REMOTE_BUILDS_SUBDIR = Path(".esphome") / ".remote_builds"

# Bundle filename inside ``<dashboard_id>/<device_name>/``.
# Constant rather than derived from the offloader's
# ``configuration_filename`` so a malicious or buggy offloader
# can't pick a name that collides with extracted artefacts (the
# YAML, the ``manifest.yaml``, etc.) — the extracted tree owns
# the rest of the directory.
_BUNDLE_FILENAME = "bundle.tar.gz"

# Allowed values of :attr:`SubmitJobFrameData.target`.
# ``Literal["compile", "upload"]`` on the TypedDict is the
# type-time gate; this set is the runtime gate so a misbehaving
# offloader sending ``target="install"`` (or anything else)
# gets a clean reject rather than a downstream JobType
# construction failure.
_TARGET_TO_JOB_TYPE: dict[str, JobType] = {
    "compile": JobType.COMPILE,
    "upload": JobType.UPLOAD,
}

# Bundle-assembler error codes that map to a clean
# ``submit_job_ack`` rejection (the offloader can fix-and-retry
# on a fresh session). Anything outside this set is wire-level
# misbehaviour the offloader can't recover from in-session and
# triggers a ``terminate{malformed_frame}`` close after the ack.
_RECOVERABLE_ASSEMBLER_ERRORS: frozenset[BundleAssemblerErrorCode] = frozenset(
    {
        BundleAssemblerErrorCode.OVERSIZED,
        BundleAssemblerErrorCode.UNDERSIZED,
        BundleAssemblerErrorCode.HASH_MISMATCH,
        BundleAssemblerErrorCode.EMPTY_BUNDLE,
    }
)


def _device_name_from_filename(configuration_filename: str) -> str:
    """Extract the device-name segment from *configuration_filename*.

    ``"kitchen.yaml"`` → ``"kitchen"``. Strips ``.yaml`` /
    ``.yml`` (case-insensitive); anything else (no extension,
    unexpected extension) returns the input unchanged so the
    rest of the validation chain catches the wire-shape problem
    rather than this helper silently producing a degenerate
    name.

    Used as the second path segment under
    ``.esphome/.remote_builds/<dashboard_id>/<device_name>/``;
    the segment is validated by :meth:`DashboardSettings.rel_path`
    downstream so a malformed entry can't escape the subtree.
    """
    lower = configuration_filename.lower()
    if lower.endswith(".yaml"):
        return configuration_filename[:-5]
    if lower.endswith(".yml"):
        return configuration_filename[:-4]
    return configuration_filename


@dataclass
class _PendingSubmit:
    """Per-session in-flight bundle reception state.

    Constructed on a valid :class:`SubmitJobFrameData` header,
    fed chunk-by-chunk from
    :meth:`SubmitJobReceiver.handle_submit_job_chunk`, and
    discarded once the submit completes or the session ends.
    Only one :class:`_PendingSubmit` exists per session at a
    time; a second header from the same session before the
    first completes is rejected as ``duplicate_submit``.
    """

    job_id: str
    configuration_filename: str
    target: str
    assembler: BundleAssembler


class SubmitJobReceiver:
    """Receiver-side state machine for the peer-link ``submit_job`` flow.

    One instance per :class:`RemoteBuildController` (started in
    :meth:`RemoteBuildController.start`). Holds per-session
    in-flight bundle reception state in :attr:`_inflight`,
    keyed on the session's ``dashboard_id``. The receive loop in
    :func:`controllers.remote_build_peer_link._receive_loop`
    forwards :attr:`AppMessageType.SUBMIT_JOB` and
    :attr:`AppMessageType.SUBMIT_JOB_CHUNK` frames to the matching
    handler method here.

    Stateless across :meth:`stop` — the controller's lifecycle
    runs at the receiver process scope, in-flight uploads don't
    survive a controller restart. A bundle that was mid-stream
    when the receiver shut down is dropped; the offloader's
    next submit attempt opens a fresh session, lands a fresh
    header, starts over.
    """

    def __init__(
        self,
        *,
        config_dir: Path,
        firmware_controller: FirmwareController,
    ) -> None:
        self._config_dir = config_dir
        self._firmware = firmware_controller
        self._inflight: dict[str, _PendingSubmit] = {}

    def discard_session(self, dashboard_id: str) -> None:
        """Drop any in-flight submit state for *dashboard_id*.

        Called when a peer-link session ends — the receive loop's
        ``finally`` chain runs ``unregister_peer_link_session``,
        which in turn calls this. A session that closed mid-stream
        leaves no buffered bytes lying around (the assembler's
        bytearray is GC'd along with the dict entry).
        """
        self._inflight.pop(dashboard_id, None)

    async def handle_submit_job(self, session: PeerLinkSession, frame: SubmitJobFrameData) -> None:
        """Validate the header, set up the assembler, register as in-flight.

        Rejects (with a typed ``submit_job_ack``) on:

        * Duplicate submit while a previous one is still in
          flight on the same session.
        * Header field shapes the wire-format TypedDict can't
          enforce at runtime (target outside the
          ``compile`` / ``upload`` set, malformed
          ``configuration_filename``).
        * Assembler-construction validation (oversized total,
          empty bundle, etc.) — these come from the announced
          header values, so they map to a ``submit_job_ack``
          rejection rather than a ``terminate{malformed_frame}``;
          the chunk stream hasn't started yet, the wire is still
          intact.
        """
        if session.dashboard_id in self._inflight:
            await self._send_ack(
                session,
                job_id=frame["job_id"],
                accepted=False,
                reason=_REASON_DUPLICATE_SUBMIT,
            )
            return

        target = frame["target"]
        if target not in _TARGET_TO_JOB_TYPE:
            await self._send_ack(
                session,
                job_id=frame["job_id"],
                accepted=False,
                reason=_REASON_INVALID_HEADER,
            )
            return

        try:
            assembler = BundleAssembler(
                total_bytes=frame["total_bundle_bytes"],
                num_chunks=frame["num_chunks"],
                sha256_hex=frame["bundle_sha256"],
            )
        except BundleAssemblerError as exc:
            await self._send_ack(
                session,
                job_id=frame["job_id"],
                accepted=False,
                reason=exc.code.value,
            )
            return

        self._inflight[session.dashboard_id] = _PendingSubmit(
            job_id=frame["job_id"],
            configuration_filename=frame["configuration_filename"],
            target=target,
            assembler=assembler,
        )

    async def handle_submit_job_chunk(  # noqa: PLR0911 — branchy by design
        self, session: PeerLinkSession, frame: SubmitJobChunkFrameData
    ) -> None:
        """Feed *frame* into the in-flight assembler. On final chunk: queue + ack.

        Branches:

        * No in-flight submit → ack reject ``no_inflight_submit``.
        * ``job_id`` doesn't match the in-flight header → ack
          reject ``job_id_mismatch`` (offloader is mixing two
          different submits onto one session).
        * Base64 decode fails → ack reject + terminate
          ``malformed_frame`` (the wire format itself is broken).
        * Recoverable assembler error
          (:data:`_RECOVERABLE_ASSEMBLER_ERRORS`) → ack reject
          with the assembler's code (offloader can retry).
        * Other assembler error → ack reject + terminate
          ``malformed_frame`` (offloader sent out-of-order /
          post-completion / chunk-count-mismatched chunks; the
          wire is corrupted, close the session).
        * Final chunk + finalise success → write bundle,
          extract, queue job, ack accepted. Failures in any of
          those steps map to ack reject without a terminate
          (the wire is fine, just our side hit an issue).
        """
        from .remote_build_peer_link import TerminateReason  # noqa: PLC0415

        pending = self._inflight.get(session.dashboard_id)
        if pending is None:
            await self._send_ack(
                session,
                job_id=frame["job_id"],
                accepted=False,
                reason=_REASON_NO_INFLIGHT,
            )
            return

        if frame["job_id"] != pending.job_id:
            await self._send_ack(
                session,
                job_id=frame["job_id"],
                accepted=False,
                reason=_REASON_JOB_ID_MISMATCH,
            )
            return

        try:
            raw = decode_chunk(frame["data_b64"])
        except (binascii.Error, ValueError):
            self._inflight.pop(session.dashboard_id, None)
            await self._send_ack(
                session,
                job_id=pending.job_id,
                accepted=False,
                reason=_REASON_CHUNK_DECODE_FAILED,
            )
            await session.terminate(TerminateReason.MALFORMED_FRAME)
            return

        try:
            pending.assembler.feed(frame["chunk_index"], raw, is_last=frame["is_last"])
        except BundleAssemblerError as exc:
            self._inflight.pop(session.dashboard_id, None)
            await self._send_ack(
                session,
                job_id=pending.job_id,
                accepted=False,
                reason=exc.code.value,
            )
            if exc.code not in _RECOVERABLE_ASSEMBLER_ERRORS:
                await session.terminate(TerminateReason.MALFORMED_FRAME)
            return

        if not frame["is_last"]:
            return

        # Final chunk landed; finalise + extract + queue. Pull the
        # entry out of ``_inflight`` first so a later mistake doesn't
        # leave a closed assembler dangling.
        self._inflight.pop(session.dashboard_id, None)
        try:
            assembled = pending.assembler.finalise()
        except BundleAssemblerError as exc:
            await self._send_ack(
                session,
                job_id=pending.job_id,
                accepted=False,
                reason=exc.code.value,
            )
            if exc.code not in _RECOVERABLE_ASSEMBLER_ERRORS:
                await session.terminate(TerminateReason.MALFORMED_FRAME)
            return

        try:
            queued_job_id = await self._extract_and_queue(
                session=session,
                pending=pending,
                bundle_bytes=assembled,
            )
        except _SubmitJobRejectionError as exc:
            await self._send_ack(
                session,
                job_id=pending.job_id,
                accepted=False,
                reason=exc.reason,
            )
            return

        # ``_extract_and_queue`` returned the queued job's id; the
        # offloader's ``job_id`` from the header is what they
        # tagged the submit with, but the receiver-side job id is
        # what every subsequent ``job_state_changed`` /
        # ``job_output`` frame keys on. Echo the offloader's
        # ``job_id`` back on the ack so the offloader can match
        # the response to its submit; the receiver-side id is
        # threaded into the 5c-2b fan-out via
        # :attr:`FirmwareJob.remote_peer` instead.
        del queued_job_id  # documented for the comment; not used yet
        await self._send_ack(
            session,
            job_id=pending.job_id,
            accepted=True,
        )

    async def _extract_and_queue(
        self,
        *,
        session: PeerLinkSession,
        pending: _PendingSubmit,
        bundle_bytes: bytes,
    ) -> str:
        """Write the tarball, extract it, queue a :class:`FirmwareJob`.

        Returns the queued job's ``job_id`` (receiver-side id,
        distinct from the offloader's submit-tagged id). Raises
        :class:`_SubmitJobRejectionError` on any failure with a
        :class:`SubmitJobAckFrameData.reason`-shaped code so the
        caller can convert into an ack reject without a terminate
        (extract / queue failures are receiver-side problems, not
        wire-level misbehaviour).

        Disk I/O hops to the executor:
        ``prepare_bundle_for_compile`` walks the tar, validates
        members, writes to disk; bundling that into one
        ``run_in_executor`` keeps the receiver's WS dispatch
        coroutine non-blocking through a multi-MB write.
        """
        device_name = _device_name_from_filename(pending.configuration_filename)
        target_dir = self._config_dir / _REMOTE_BUILDS_SUBDIR / session.dashboard_id / device_name
        bundle_path = target_dir / _BUNDLE_FILENAME

        loop = asyncio.get_running_loop()
        try:
            extracted_yaml: Path = await loop.run_in_executor(
                None,
                _write_and_extract_bundle,
                bundle_path,
                bundle_bytes,
                target_dir,
            )
        except (EsphomeError, OSError) as exc:
            _LOGGER.warning(
                "submit_job from %s: extract failed for job %s (%s): %s",
                session.dashboard_id,
                pending.job_id,
                pending.configuration_filename,
                exc,
            )
            raise _SubmitJobRejectionError(_REASON_EXTRACT_FAILED) from exc

        # ``configuration`` on FirmwareJob is the path relative to
        # the controller's config_dir (``rel_path`` joins back on
        # the way out). The extracted YAML lives under
        # ``.esphome/.remote_builds/<dashboard_id>/<device_name>/``
        # inside the same config_dir, so ``relative_to`` always
        # succeeds here.
        rel_yaml = extracted_yaml.relative_to(self._config_dir)
        configuration = str(rel_yaml)

        try:
            job = self._firmware._create_job(
                configuration=configuration,
                job_type=_TARGET_TO_JOB_TYPE[pending.target],
                remote_peer=session.dashboard_id,
            )
            await self._firmware._enqueue(job)
        except Exception as exc:
            _LOGGER.warning(
                "submit_job from %s: enqueue failed for job %s: %s",
                session.dashboard_id,
                pending.job_id,
                exc,
            )
            raise _SubmitJobRejectionError(_REASON_QUEUE_REJECTED) from exc

        _LOGGER.info(
            "submit_job from %s: queued job %s (%s, target=%s)",
            session.dashboard_id,
            job.job_id,
            configuration,
            pending.target,
        )
        return job.job_id

    async def _send_ack(
        self,
        session: PeerLinkSession,
        *,
        job_id: str,
        accepted: bool,
        reason: str | None = None,
    ) -> None:
        """Send a typed :class:`SubmitJobAckFrameData` to *session*.

        ``reason`` is omitted from the wire payload on the
        success path so the frame is exactly
        ``{type, job_id, accepted: true}`` (matching the
        ``NotRequired`` declaration on the TypedDict). Failures
        from ``send_app_frame`` are logged at the channel layer
        and don't propagate here — the session is already
        closing or gone, the ack going missing isn't actionable.
        """
        payload: SubmitJobAckFrameData = (
            SubmitJobAckFrameData(
                type="submit_job_ack",
                job_id=job_id,
                accepted=accepted,
            )
            if reason is None
            else SubmitJobAckFrameData(
                type="submit_job_ack",
                job_id=job_id,
                accepted=accepted,
                reason=reason,
            )
        )
        await session.send_app_frame(dict(payload))


class _SubmitJobRejectionError(Exception):
    """Internal: surface a typed rejection reason out of ``_extract_and_queue``.

    Carries a :class:`SubmitJobAckFrameData.reason`-shaped
    string. Caught by :meth:`SubmitJobReceiver.handle_submit_job_chunk`
    on the final-chunk path and converted to an ack reject; never
    leaks past that boundary.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _write_and_extract_bundle(bundle_path: Path, bundle_bytes: bytes, target_dir: Path) -> Path:
    """Sync helper run in the executor: write the tarball + extract it.

    Bundled together so the
    :meth:`SubmitJobReceiver._extract_and_queue` callsite hops
    to the executor exactly once for both phases.
    ``prepare_bundle_for_compile`` is the entry point because it
    preserves the build-cache subdirectories (``.esphome`` /
    ``.pioenvs``) that incremental compiles need to skip the
    cold-rebuild hit.
    """
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_bytes(bundle_bytes)
    extracted: Path = prepare_bundle_for_compile(bundle_path, target_dir)
    return extracted
