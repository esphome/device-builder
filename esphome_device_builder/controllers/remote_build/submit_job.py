"""
Receiver-side ``submit_job`` flow for the remote-build peer-link.

Drives the post-handshake ``submit_job`` header +
``submit_job_chunk`` stream from the peer-link receive loop into
a queued :class:`FirmwareJob` carrying the offloader's
``dashboard_id`` in :attr:`FirmwareJob.remote_peer`. This module
ends at "ack the bundle and queue the job"; the lifecycle
fan-out the other direction — pushing ``job_state_changed`` /
``job_output`` frames over the submitting session — lives in
:mod:`.job_fanout`.

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
   (sibling of the per-device subtree, not child — see
   :class:`helpers.remote_build_layout.RemoteBuildPath` for the
   canonical layout),
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

from ...helpers.lazy_module import async_import_module
from ...helpers.peer_link_bundle import (
    BundleAssembler,
    BundleAssemblerError,
    BundleAssemblerErrorCode,
    decode_chunk,
)
from ...helpers.peer_link_frames import frame_schema, is_valid_frame
from ...helpers.remote_build_layout import REMOTE_BUILDS_SUBDIR, RemoteBuildPath
from ...models import (
    JobType,
    SubmitJobAckFrameData,
    SubmitJobChunkFrameData,
    SubmitJobFrameData,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..firmware import FirmwareController
    from .peer_link import PeerLinkSession

_LOGGER = logging.getLogger(__name__)

# Reject reason codes carried on
# :class:`SubmitJobAckFrameData.reason` when ``accepted=False``.
# Distinct from :class:`BundleAssemblerErrorCode` (wire-level
# bundle problems): these cover the receiver-side dispatch path
# where the bundle assembled cleanly but something else went
# wrong (path traversal, extraction failure, queue rejection).
# The offloader's submitter maps these to user-facing error
# messages.
_REASON_DUPLICATE_SUBMIT = "duplicate_submit"
_REASON_INVALID_HEADER = "invalid_header"
_REASON_INVALID_CHUNK = "invalid_chunk"
_REASON_NO_INFLIGHT = "no_inflight_submit"
_REASON_JOB_ID_MISMATCH = "job_id_mismatch"
_REASON_CHUNK_DECODE_FAILED = "chunk_decode_failed"
_REASON_EXTRACT_FAILED = "extract_failed"
_REASON_QUEUE_REJECTED = "queue_rejected"

# Cap on the peer-controlled display strings the header carries
# (``device_name`` / ``device_friendly_name``). The schema gate
# leaves these fields untyped, so a malicious / buggy offloader
# could ship a non-string or a multi-megabyte string that we'd
# end up stamping onto :class:`FirmwareJob` and replaying through
# the firmware-tasks WS stream. 256 chars is twice
# :data:`StoredPairing._MAX_LABEL_LEN` (128) and well above the
# longest reasonable device / friendly name anyone would write
# in YAML — values above the cap get truncated to empty rather
# than rejected, since the field is display-only and an empty
# title gracefully falls back to the configuration path.
_DEVICE_DISPLAY_FIELD_MAX_LEN = 256

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

# Layout for the per-dashboard / per-device subtree + sibling
# bundle tarball lives in :mod:`helpers.remote_build_layout` so
# the writer here, the 6c TTL sweep, and the controller's
# in-flight-key derivation all flow through one source of
# truth. See that module's :class:`RemoteBuildPath` for the
# canonical key shape.

# Allowed values of :attr:`SubmitJobFrameData.target`.
# ``Literal["compile", "upload", "clean"]`` on the TypedDict is
# the type-time gate; this set is the runtime gate so a
# misbehaving offloader sending ``target="install"`` (or anything
# else) gets a clean reject rather than a downstream JobType
# construction failure.
#
# ``target="clean"`` rides the same submit_job pipeline as
# compile / upload — receiver re-extracts the YAML to the
# per-offloader subtree, then runs ``esphome clean`` against it,
# which wipes ``<data_dir>/build/<device_name>/``. The receiver's
# 6c TTL sweep eventually reclaims the subtree itself; an
# explicit clean is about freeing the shared per-device build
# tree, not the per-offloader sidecar. The offloader fans out
# clean to every connected peer when the operator clicks "Clean
# build files" so receivers that have built this device locally
# also drop their stale artifacts.
_TARGET_TO_JOB_TYPE: dict[str, JobType] = {
    "compile": JobType.COMPILE,
    "upload": JobType.UPLOAD,
    "clean": JobType.CLEAN,
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


def _coerce_display_field(value: Any) -> str:
    """Coerce a peer-supplied display string to a safe, bounded ``str``.

    The ``NotRequired`` ``device_name`` / ``device_friendly_name``
    fields on :class:`SubmitJobFrameData` bypass the schema gate
    (the gate validates a known-keys subset; extras pass
    through), so a non-``str`` value or a multi-megabyte string
    from a malicious / buggy offloader would otherwise reach
    the in-flight :class:`_PendingSubmit` and land on the
    :class:`FirmwareJob` we replay through the firmware-tasks
    WS stream. Soft-coerce rather than reject:

    * Non-``str`` → ``""``. The display surface treats empty as
      "fall back to the configuration path" — a clear UI signal
      vs. a hard reject the operator can't recover from.
    * Length > :data:`_DEVICE_DISPLAY_FIELD_MAX_LEN` → ``""``,
      same rationale. The cap is well above any legitimate
      device / friendly name; values past it are signalling
      abuse, not a long but legitimate string.

    The display fields are UI plumbing, not load-bearing for the
    build (the path-level gates in
    :func:`_validate_configuration_filename` are what keep the
    extract step safe). Empty fallback is the safe default.
    """
    if not isinstance(value, str):
        return ""
    if len(value) > _DEVICE_DISPLAY_FIELD_MAX_LEN:
        return ""
    return value


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
    # Display strings carried on the SUBMIT_JOB header; empty
    # for older offloaders that don't set the (NotRequired)
    # wire fields. The receiver stamps both onto the
    # :class:`FirmwareJob` so the firmware-tasks UI renders the
    # device's actual name + friendly name instead of the
    # ``.esphome/.remote_builds/<id>/<device>/<device>.yaml``
    # path. No semantic meaning beyond display: the path-level
    # security gate (``_validate_configuration_filename``) is
    # what keeps the receiver safe; these fields are purely UI
    # plumbing.
    device_name: str = ""
    device_friendly_name: str = ""


class SubmitJobReceiver:
    """Receiver-side state machine for the peer-link ``submit_job`` flow.

    One instance per :class:`ReceiverController` (started in
    :meth:`ReceiverController.start`). Holds per-session
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
            # Coerce + cap the peer-controlled display strings.
            # The schema gate leaves these ``NotRequired`` fields
            # untyped at the wire boundary, so a non-string or an
            # oversized string would otherwise reach the
            # :class:`FirmwareJob` and the WS stream. Soft-coerce
            # to ``""`` rather than rejecting the submit — the
            # display fields are UI plumbing, not load-bearing
            # for the build.
            device_name=_coerce_display_field(frame.get("device_name")),
            device_friendly_name=_coerce_display_field(frame.get("device_friendly_name")),
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
        # receiver-side job id is threaded into the fan-out
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
        the fan-out path; the offloader echoes against its
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
        # Subtree (extract target) + bundle (sibling tarball)
        # flow through the layout helper so the writer here and
        # the 6c sweeper read one source of truth for the
        # on-disk shape. Sibling-not-child is load-bearing:
        # upstream prepare_bundle_for_compile wipes target_dir
        # before extract_bundle reads from bundle_path, so a
        # bundle inside target_dir would be deleted mid-flow
        # (PR #552).
        key = RemoteBuildPath(dashboard_id=session.dashboard_id, device_name=device_name)
        target_dir = key.subtree(self._config_dir)
        bundle_path = key.bundle(self._config_dir)
        remote_builds_root = self._config_dir / REMOTE_BUILDS_SUBDIR

        # ``esphome.bundle`` is ~1 MB of upstream code; load it through
        # the shared lazy-import executor so the receiver's idle resident
        # set stays lean until a peer-link offload actually lands. Pass
        # the resolved callable into the executor so the worker doesn't
        # rely on a preload-ordering invariant in this caller.
        bundle = await async_import_module("esphome.bundle")
        prepare = bundle.prepare_bundle_for_compile
        loop = asyncio.get_running_loop()
        try:
            configuration = await loop.run_in_executor(
                None,
                _validate_write_extract_bundle,
                bundle_path,
                bundle_bytes,
                target_dir,
                remote_builds_root,
                self._config_dir,
                prepare,
            )
        except _PathEscapeError as exc:
            _LOGGER.warning(
                "submit_job from %s: target_dir %s escaped remote-builds root; rejecting",
                session.dashboard_id,
                target_dir,
            )
            raise _SubmitJobRejectionError(_REASON_INVALID_HEADER) from exc
        except (bundle.EsphomeError, OSError) as exc:
            _LOGGER.warning(
                "submit_job from %s: extract failed for job %s (%s): %s",
                session.dashboard_id,
                pending.job_id,
                pending.configuration_filename,
                exc,
            )
            raise _SubmitJobRejectionError(_REASON_EXTRACT_FAILED) from exc

        # Snapshot the offloader's display label so the
        # firmware-tasks UI can render "from {label}" without
        # re-looking-up the (potentially since-renamed) peer.
        # Goes through :meth:`ReceiverController.approved_peer_label`
        # so the receiver doesn't couple to the private
        # ``_approved_peers`` layout — a future refactor of the
        # peer registry (e.g. moving APPROVED rows into a per-
        # file ``Store`` like ``_pairings``) only has to keep
        # the accessor's contract.
        remote_peer_label = ""
        receiver = self._firmware._db.remote_build_receiver
        if receiver is not None:
            remote_peer_label = receiver.approved_peer_label(session.dashboard_id)

        try:
            job = self._firmware._create_job(
                configuration=configuration,
                job_type=_TARGET_TO_JOB_TYPE[pending.target],
                remote_peer=session.dashboard_id,
                remote_peer_label=remote_peer_label,
                remote_job_id=pending.job_id,
                # ``device_name`` / ``device_friendly_name`` come
                # off the wire header — the offloader already
                # knows both from its local Device list at install
                # time, so the receiver doesn't re-parse the
                # bundled YAML just to render a title. Defaults
                # to ``""`` for older offloaders that don't set
                # the (NotRequired) fields; the frontend's title
                # then falls back to the configuration path.
                device_name=pending.device_name,
                device_friendly_name=pending.device_friendly_name,
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
    config_dir: Path,
    prepare_bundle_for_compile: Callable[[Path, Path], Path],
) -> str:
    """Sync helper: validate path, write tarball, extract, return wire-shape ``configuration``.

    All four steps run in the executor so the receiver's WS
    dispatch coroutine stays non-blocking through every
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
    ``.pioenvs`` for incremental compiles). (4) Compute the
    POSIX-relative ``configuration`` string for the
    :class:`FirmwareJob` wire field, resolving *config_dir* so a
    receiver started with a relative configuration arg
    (#678) still produces an absolute-vs-absolute
    ``relative_to`` pair.

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
    # ``as_posix`` keeps the wire-side ``configuration`` string
    # stable across receiver platforms — ``str(rel_yaml)`` would
    # emit ``\\``-separated paths on Windows.
    return extracted.relative_to(config_dir.resolve()).as_posix()
