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
   ``<config>/.esphome/.remote_builds/<dashboard_id>/<device_name>.tar.gz``
   (sibling of the per-device subtree, not child — see the
   ``_BUNDLE_SUFFIX`` comment for why),
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
from typing import TYPE_CHECKING, Any, cast

from esphome.bundle import EsphomeError, prepare_bundle_for_compile

from ...helpers.peer_link_bundle import (
    BundleAssembler,
    BundleAssemblerError,
    BundleAssemblerErrorCode,
    decode_chunk,
)
from ...helpers.peer_link_frames import frame_schema, is_valid_frame
from ...models import (
    JobType,
    SubmitJobAckFrameData,
    SubmitJobChunkFrameData,
    SubmitJobFrameData,
)

if TYPE_CHECKING:
    from ..firmware import FirmwareController
    from .peer_link import PeerLinkSession

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
_REASON_INVALID_CHUNK = "invalid_chunk"
_REASON_NO_INFLIGHT = "no_inflight_submit"
_REASON_JOB_ID_MISMATCH = "job_id_mismatch"
_REASON_CHUNK_DECODE_FAILED = "chunk_decode_failed"
_REASON_EXTRACT_FAILED = "extract_failed"
_REASON_QUEUE_REJECTED = "queue_rejected"

# Shape contracts for the two peer-controlled wire frames.
# :func:`parse_app_frame` already confirms inbound bytes parse
# to a ``dict[str, Any]``, but a malicious / buggy offloader
# can still send a dict with missing fields or wrong-typed
# values. Indexing those frames directly (``frame["job_id"]``,
# etc.) would raise ``KeyError`` / ``TypeError`` and unwind out
# of the receive loop without sending an ack — a remote-
# triggered crash shape. The :func:`is_valid_frame` gate below
# walks each schema and rejects the frame as
# ``invalid_header`` / ``invalid_chunk`` with a
# ``terminate{malformed_frame}`` close (the offloader has
# wandered off the wire format).
_SUBMIT_JOB_HEADER_SCHEMA = frame_schema(
    {
        "job_id": str,
        "configuration_filename": str,
        "target": str,
        "total_bundle_bytes": int,
        "num_chunks": int,
        "bundle_sha256": str,
    }
)

_SUBMIT_JOB_CHUNK_SCHEMA = frame_schema(
    {
        "job_id": str,
        "chunk_index": int,
        "data_b64": str,
        "is_last": bool,
    }
)

# Subdirectory under ``<config_dir>/.esphome/`` where remote-peer
# bundles land. Hidden by the leading dot so a casual ``ls`` of
# the user's main config tree doesn't show it next to their own
# YAMLs; living under ``.esphome/`` keeps it adjacent to other
# build artefacts (StorageJSON, build dirs) so phase-6's TTL
# sweep can reuse the same parent walk.
_REMOTE_BUILDS_SUBDIR = Path(".esphome") / ".remote_builds"

# Bundle filename used as a sibling of the extract target_dir
# (``<dashboard_id>/<device_name>.tar.gz``, next to the
# ``<dashboard_id>/<device_name>/`` build subtree). Living
# outside target_dir is load-bearing: upstream
# :func:`prepare_bundle_for_compile` preserves only
# ``.esphome`` / ``.pioenvs`` / ``.pio`` in target_dir and
# wipes every other entry before extract; a bundle inside
# target_dir would be deleted between the path-resolved
# ``is_file`` check and the inner ``extract_bundle`` call,
# surfacing as "Bundle file not found" at compile time.
# Derived from *device_name* (not the offloader's raw
# ``configuration_filename``) because ``_validate_configuration_filename``
# already canonicalised it; using the canonical form keeps a
# malicious sender from picking a path-traversal shape that
# climbs out of the dashboard_id namespace.
_BUNDLE_SUFFIX = ".tar.gz"

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


# Characters that must NOT appear in a peer-supplied
# ``configuration_filename``. Path separators (both flavours so
# the rule holds across receiver platforms) and the NUL byte.
# The rule's job is to catch obviously-malicious shapes early;
# the resolve-and-stay-under-root check at extract time is the
# defence-in-depth gate that catches anything an exotic filename
# would slip past this.
_FORBIDDEN_FILENAME_CHARS: frozenset[str] = frozenset({"/", "\\", "\x00"})


def _validate_configuration_filename(filename: str) -> str | None:
    r"""Return the device-name segment if *filename* is a safe leaf YAML, else ``None``.

    Peer-supplied input. The receiver uses the device-name
    segment (``filename`` minus its ``.yaml`` / ``.yml``
    extension) as the second path component under
    ``<config>/.esphome/.remote_builds/<dashboard_id>/<device>/``;
    a malicious offloader sending ``../foo.yaml`` could escape
    that subtree without this gate. Returning ``None`` signals
    the caller should reject with ``invalid_header``.

    Rejects:

    * Empty / non-string input.
    * Path separators (``/`` or ``\\``) or NUL bytes.
    * Reserved names ``"."`` / ``".."`` (with or without
      extension — ``..yaml`` is still a leading-dot escape
      attempt).
    * Anything that doesn't end in ``.yaml`` / ``.yml``
      (case-insensitive). The bundle the receiver extracts is
      an ESPHome YAML config; non-YAML extensions don't have a
      legitimate use here and let a misbehaving offloader
      write arbitrary suffixes into the per-peer subtree.

    Returns the bare device name (``"kitchen.yaml"`` →
    ``"kitchen"``) on success.
    """
    if not filename:
        return None
    if any(ch in filename for ch in _FORBIDDEN_FILENAME_CHARS):
        return None
    lower = filename.lower()
    if lower.endswith(".yaml"):
        device_name = filename[:-5]
    elif lower.endswith(".yml"):
        device_name = filename[:-4]
    else:
        return None
    # Reject a leaf whose pre-extension stem reduces to ``.`` /
    # ``..`` — both would resolve to the parent dir under
    # ``<config_dir>/.esphome/.remote_builds/<dashboard_id>/``.
    if device_name in ("", ".", ".."):
        return None
    return device_name


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
        # Validate the wire-frame shape before indexing
        # peer-controlled fields. A malformed frame is wire-
        # level misbehaviour and triggers a
        # ``terminate{malformed_frame}``; ``job_id`` may itself
        # be missing/wrong-typed so fall back to ``""`` for the
        # ack payload. ``cast`` to ``dict[str, Any]`` because
        # the validator works on the raw shape; the typed
        # ``SubmitJobFrameData`` view is what the rest of the
        # method operates on after the gate.
        raw = cast(dict[str, Any], frame)
        if not is_valid_frame(_SUBMIT_JOB_HEADER_SCHEMA, raw):
            job_id = raw.get("job_id") if isinstance(raw.get("job_id"), str) else ""
            await self._reject(
                session,
                job_id=cast(str, job_id),
                reason=_REASON_INVALID_HEADER,
                terminate_session=True,
            )
            return
        if session.dashboard_id in self._inflight:
            await self._reject(session, job_id=frame["job_id"], reason=_REASON_DUPLICATE_SUBMIT)
            return
        target = frame["target"]
        if target not in _TARGET_TO_JOB_TYPE:
            await self._reject(session, job_id=frame["job_id"], reason=_REASON_INVALID_HEADER)
            return
        # Validate the peer-supplied filename — it becomes the
        # second path segment under
        # ``.esphome/.remote_builds/<dashboard_id>/<device_name>/``.
        # An unvalidated separator / ``..`` here would let a
        # malicious offloader write the assembled tarball
        # outside the intended subtree.
        if _validate_configuration_filename(frame["configuration_filename"]) is None:
            await self._reject(session, job_id=frame["job_id"], reason=_REASON_INVALID_HEADER)
            return
        try:
            assembler = BundleAssembler(
                total_bytes=frame["total_bundle_bytes"],
                num_chunks=frame["num_chunks"],
                sha256_hex=frame["bundle_sha256"],
            )
        except BundleAssemblerError as exc:
            await self._reject(session, job_id=frame["job_id"], reason=exc.code.value)
            return

        self._inflight[session.dashboard_id] = _PendingSubmit(
            job_id=frame["job_id"],
            configuration_filename=frame["configuration_filename"],
            target=target,
            assembler=assembler,
        )

    async def handle_submit_job_chunk(
        self, session: PeerLinkSession, frame: SubmitJobChunkFrameData
    ) -> None:
        """Feed *frame* into the in-flight assembler. On final chunk: queue + ack.

        Reject branches all flow through :meth:`_reject` with a
        ``reason`` code; the helper drops in-flight state and
        optionally fires ``terminate{malformed_frame}`` based on
        whether the failure is wire-level (offloader corrupted
        the stream — close the session) or recoverable (offloader
        can retry on a fresh submit). Happy-path completion
        flows through :meth:`_finalise_and_queue`.
        """
        # Same shape gate as the header path: peer-controlled
        # fields must be present and correctly typed before any
        # indexing. A malformed chunk is wire-level misbehaviour
        # and the in-flight stream can't be recovered; drop it
        # and terminate.
        chunk_dict = cast(dict[str, Any], frame)
        if not is_valid_frame(_SUBMIT_JOB_CHUNK_SCHEMA, chunk_dict):
            job_id = chunk_dict.get("job_id") if isinstance(chunk_dict.get("job_id"), str) else ""
            await self._reject(
                session,
                job_id=cast(str, job_id),
                reason=_REASON_INVALID_CHUNK,
                drop_inflight=True,
                terminate_session=True,
            )
            return
        pending = self._inflight.get(session.dashboard_id)
        if pending is None:
            await self._reject(session, job_id=frame["job_id"], reason=_REASON_NO_INFLIGHT)
            return
        if frame["job_id"] != pending.job_id:
            await self._reject(session, job_id=frame["job_id"], reason=_REASON_JOB_ID_MISMATCH)
            return
        try:
            raw = decode_chunk(frame["data_b64"])
        except (binascii.Error, ValueError):
            await self._reject(
                session,
                job_id=pending.job_id,
                reason=_REASON_CHUNK_DECODE_FAILED,
                drop_inflight=True,
                terminate_session=True,
            )
            return
        try:
            pending.assembler.feed(frame["chunk_index"], raw, is_last=frame["is_last"])
        except BundleAssemblerError as exc:
            await self._reject_assembler(session, pending=pending, exc=exc)
            return
        if not frame["is_last"]:
            return
        await self._finalise_and_queue(session=session, pending=pending)

    async def _finalise_and_queue(
        self, *, session: PeerLinkSession, pending: _PendingSubmit
    ) -> None:
        """Pull the in-flight entry, finalise the bundle, extract + queue + ack.

        Split out from :meth:`handle_submit_job_chunk` so the
        final-chunk path is read-on-its-own rather than tail-of-
        a-flat-cascade. Drops the in-flight entry first so any
        later failure can't leave a closed assembler dangling.
        """
        self._inflight.pop(session.dashboard_id, None)
        try:
            assembled = pending.assembler.finalise()
        except BundleAssemblerError as exc:
            await self._reject_assembler(session, pending=pending, exc=exc)
            return
        try:
            await self._extract_and_queue(session=session, pending=pending, bundle_bytes=assembled)
        except _SubmitJobRejectionError as exc:
            await self._reject(session, job_id=pending.job_id, reason=exc.reason)
            return
        # Echo the offloader's ``job_id`` back on the ack so the
        # offloader can match the response to its submit; the
        # receiver-side job id is threaded into the 5c-2b fan-out
        # via :attr:`FirmwareJob.remote_peer` instead.
        await self._send_ack_accepted(session, job_id=pending.job_id)

    async def _reject_assembler(
        self,
        session: PeerLinkSession,
        *,
        pending: _PendingSubmit,
        exc: BundleAssemblerError,
    ) -> None:
        """Reject helper for assembler errors — terminates on wire-level codes only.

        Codes in :data:`_RECOVERABLE_ASSEMBLER_ERRORS`
        (``oversized`` / ``undersized`` / ``hash_mismatch`` /
        ``empty_bundle``) ack-and-stay so the offloader can
        retry on a fresh submit. Anything else (out-of-order,
        post-completion, chunk-count-mismatched) is wire-level
        misbehaviour and triggers a
        ``terminate{malformed_frame}`` close after the ack.
        """
        await self._reject(
            session,
            job_id=pending.job_id,
            reason=exc.code.value,
            drop_inflight=True,
            terminate_session=exc.code not in _RECOVERABLE_ASSEMBLER_ERRORS,
        )

    async def _extract_and_queue(
        self,
        *,
        session: PeerLinkSession,
        pending: _PendingSubmit,
        bundle_bytes: bytes,
    ) -> None:
        """Write the tarball, extract it, queue a :class:`FirmwareJob`.

        Raises :class:`_SubmitJobRejectionError` on any failure
        with a :class:`SubmitJobAckFrameData.reason`-shaped code
        so the caller can convert into an ack reject without a
        terminate (extract / queue failures are receiver-side
        problems, not wire-level misbehaviour). The receiver-side
        job id is captured in :attr:`FirmwareJob.remote_peer` for
        the 5c-2b fan-out path; the offloader echoes against its
        own submit-tagged ``job_id`` rather than the receiver's
        local one.

        Disk I/O hops to the executor:
        ``prepare_bundle_for_compile`` walks the tar, validates
        members, writes to disk; bundling that into one
        ``run_in_executor`` keeps the receiver's WS dispatch
        coroutine non-blocking through a multi-MB write.
        """
        # ``device_name`` is guaranteed non-None here:
        # :meth:`handle_submit_job` rejected the header upfront
        # if validation failed, so a ``_PendingSubmit`` exists
        # only for filenames that already passed the gate.
        device_name = _validate_configuration_filename(pending.configuration_filename)
        assert device_name is not None  # narrowed by the upstream reject
        remote_builds_root = self._config_dir / _REMOTE_BUILDS_SUBDIR
        target_dir = remote_builds_root / session.dashboard_id / device_name
        # Sibling of target_dir, not child — see _BUNDLE_SUFFIX
        # comment for why this matters.
        bundle_path = target_dir.parent / f"{device_name}{_BUNDLE_SUFFIX}"

        loop = asyncio.get_running_loop()
        try:
            extracted_yaml: Path = await loop.run_in_executor(
                None,
                _validate_write_extract_bundle,
                bundle_path,
                bundle_bytes,
                target_dir,
                remote_builds_root,
            )
        except _PathEscapeError as exc:
            _LOGGER.warning(
                "submit_job from %s: target_dir %s escaped remote-builds root; rejecting",
                session.dashboard_id,
                target_dir,
            )
            raise _SubmitJobRejectionError(_REASON_INVALID_HEADER) from exc
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
        # succeeds here. ``as_posix`` keeps the wire-side
        # ``configuration`` string stable across receiver
        # platforms — ``str(rel_yaml)`` would emit
        # ``\\``-separated paths on Windows, drifting the
        # ``FirmwareJob.configuration`` field's shape between a
        # dashboard running on Linux vs Windows even though the
        # filesystem-level join works either way.
        rel_yaml = extracted_yaml.relative_to(self._config_dir)
        configuration = rel_yaml.as_posix()

        try:
            job = self._firmware._create_job(
                configuration=configuration,
                job_type=_TARGET_TO_JOB_TYPE[pending.target],
                remote_peer=session.dashboard_id,
                remote_job_id=pending.job_id,
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

    async def _send_ack_accepted(self, session: PeerLinkSession, *, job_id: str) -> None:
        """Send the success-path ``submit_job_ack`` (no ``reason`` field)."""
        payload = SubmitJobAckFrameData(type="submit_job_ack", job_id=job_id, accepted=True)
        await session.send_app_frame(dict(payload))

    async def _reject(
        self,
        session: PeerLinkSession,
        *,
        job_id: str,
        reason: str,
        drop_inflight: bool = False,
        terminate_session: bool = False,
    ) -> None:
        """Single chokepoint for every reject path.

        Drops the in-flight entry when *drop_inflight* is true
        (the failure leaves no recoverable in-flight state, e.g.
        decode / assembler errors mid-stream), sends a typed
        ``submit_job_ack`` with ``accepted=False`` + the given
        *reason*, then optionally fires
        ``terminate{malformed_frame}`` on the session when the
        failure was wire-level misbehaviour (out-of-order
        chunks, base64 garbage). Receiver-side problems
        (``extract_failed`` / ``queue_rejected`` / header
        validation that didn't reach an assembler) leave the
        session intact so the offloader can retry on a fresh
        submit.

        Failures from ``send_app_frame`` are logged at the
        channel layer and don't propagate here — the session
        is already closing or gone, the ack going missing
        isn't actionable.
        """
        # Local import sidesteps the circular dep:
        # ``remote_build_peer_link`` imports symbols from this
        # module via :class:`SubmitJobReceiver`-shaped duck
        # typing in its receive loop, but only the
        # ``TerminateReason`` enum reads back the other way.
        from .peer_link import TerminateReason  # noqa: PLC0415

        if drop_inflight:
            self._inflight.pop(session.dashboard_id, None)
        payload = SubmitJobAckFrameData(
            type="submit_job_ack", job_id=job_id, accepted=False, reason=reason
        )
        await session.send_app_frame(dict(payload))
        if terminate_session:
            await session.terminate(TerminateReason.MALFORMED_FRAME)


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


class _PathEscapeError(Exception):
    """*target_dir* resolved outside the remote-builds root.

    Surfaced from :func:`_validate_write_extract_bundle` so the
    caller can map to a typed
    :class:`SubmitJobAckFrameData.reason` of ``invalid_header``.
    Distinct from the ``EsphomeError`` / ``OSError`` paths
    (which surface as ``extract_failed``) because this is a
    wire-shape problem — the offloader's ``configuration_filename``
    or its captured ``dashboard_id`` carries a path-traversal
    shape — not a receiver-side I/O failure.
    """


def _validate_write_extract_bundle(
    bundle_path: Path,
    bundle_bytes: bytes,
    target_dir: Path,
    remote_builds_root: Path,
) -> Path:
    """Sync helper: validate path is under root, write tarball, extract.

    All three steps run in the executor so the receiver's WS
    dispatch coroutine stays non-blocking through both the
    ``Path.resolve`` walk (which calls ``os.path.realpath``,
    which calls the blocking ``os.path.abspath`` syscall) and
    the multi-MB tarball write. ``Path.resolve`` is a stat-y
    syscall; it has to run in a thread.

    Validation order: (1) resolve-and-stay-under-root check
    *before* writing anything to disk so a malicious
    ``configuration_filename`` or ``dashboard_id`` can't
    materialise even an empty tarball outside the remote-builds
    subtree. (2) Write the tarball. (3) Extract via
    ``prepare_bundle_for_compile`` (preserves ``.esphome`` /
    ``.pioenvs`` for incremental compiles).

    Raises :class:`_PathEscapeError` on the path-escape branch
    so the caller can distinguish "bad input shape" from
    "extract failed". Raises
    :class:`esphome.bundle.EsphomeError` / :class:`OSError`
    untouched for the extract / write paths.
    """
    # Resolve-and-stay-under-root. ``Path.resolve()`` normalises
    # ``..`` / symlinks; ``relative_to`` raises ``ValueError``
    # when the result climbs outside the remote-builds root.
    # The upstream filename validator catches separator / ``..``
    # in ``configuration_filename`` upfront, but ``dashboard_id``
    # flows through unvalidated from the Noise handshake /
    # receiver-side registration; this gate catches anything an
    # exotic ``dashboard_id`` shape would slip past.
    try:
        target_dir.resolve().relative_to(remote_builds_root.resolve())
    except ValueError as exc:
        raise _PathEscapeError(str(target_dir)) from exc
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_path.write_bytes(bundle_bytes)
    extracted: Path = prepare_bundle_for_compile(bundle_path, target_dir)
    return extracted
