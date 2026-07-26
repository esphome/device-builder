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

import binascii
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ....helpers.peer_link_bundle import (
    BundleAssembler,
    BundleAssemblerError,
    BundleAssemblerErrorCode,
    decode_chunk,
)
from ....helpers.peer_link_frames import frame_schema, is_valid_frame, safe_job_id
from ....helpers.version_compat import coerce_pep440_version
from ....models import (
    PAIRING_VERSION_MAX_LEN,
    SubmitJobAckFrameData,
    SubmitJobChunkFrameData,
    SubmitJobFrameData,
)
from . import _post_ack
from ._extract_window import ExtractWindow
from .const import (
    REASON_CHUNK_DECODE_FAILED,
    REASON_DUPLICATE_SUBMIT,
    REASON_INVALID_CHUNK,
    REASON_INVALID_HEADER,
    REASON_JOB_ID_MISMATCH,
    REASON_NO_INFLIGHT,
    REASON_UPLOAD_UNSUPPORTED,
    TARGET_TO_JOB_TYPE,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ....models import FirmwareJob
    from ...firmware import FirmwareController
    from ..peer_link import PeerLinkSession

_LOGGER = logging.getLogger(__name__)

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


def _coerce_version_field(value: Any) -> str:
    """Coerce the peer-supplied ``target_esphome_version`` to a PEP 440 string or ``""``.

    ``NotRequired`` on the wire, so a non-str / malformed / oversized / injected
    value would otherwise reach the provisioner's ``pip install`` argument; soft
    coerce to ``""`` (no provisioning, compile with the installed esphome).
    """
    return coerce_pep440_version(value, max_len=PAIRING_VERSION_MAX_LEN)


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
    # Validated YAML stem off ``configuration_filename``; keys the
    # on-disk extract subtree.
    device_stem: str
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
    # Offloader's esphome version off the SUBMIT_JOB header; empty for
    # older offloaders. The receiver provisions a matching esphome venv
    # to compile with when it differs from its own installed version.
    target_esphome_version: str = ""


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

    Nothing survives :meth:`stop` — it cancels any in-flight
    post-ack extract tasks, and a bundle that was mid-stream
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
        self._extracts = ExtractWindow()

    def has_any_inflight(self) -> bool:
        """Whether any offloader has a bundle mid-upload or mid-extract (the reset busy gate)."""
        return bool(self._inflight) or self._extracts.active

    async def stop(self) -> None:
        """Refuse new post-ack extracts, then cancel and drain the in-flight ones."""
        await self._extracts.stop()

    def cancel_extract(self, dashboard_id: str, job_id: str) -> bool:
        """
        Flag a mid-extract submit for cancellation.

        True when a live extract holds the correlation and will report the
        terminal; False once the enqueue handoff has made the job resolvable
        through the fan-out.
        """
        return self._extracts.cancel((dashboard_id, job_id))

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
          flight on the same session, or while the same
          ``job_id``'s accepted bundle is still mid-extract.
        * Header field shapes the wire-format TypedDict can't
          enforce at runtime (target outside the
          ``compile`` / ``clean`` set, malformed
          ``configuration_filename``), plus the explicit
          ``upload_unsupported`` reject.
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
            await self._reject(
                session,
                job_id=safe_job_id(raw),
                reason=REASON_INVALID_HEADER,
                terminate_session=True,
            )
            return
        job_id = frame["job_id"]
        # A resubmit of a job_id whose accepted bundle is still mid-extract
        # would overwrite the extract index and mis-route a cancel.
        if session.dashboard_id in self._inflight or self._extracts.is_tracked(
            (session.dashboard_id, job_id)
        ):
            await self._reject(session, job_id=job_id, reason=REASON_DUPLICATE_SUBMIT)
            return
        target = frame["target"]
        if target == "upload":
            # Distinct from ``invalid_header`` so an older offloader's
            # dialog surfaces a recognisable refusal, not "bad frame".
            await self._reject(session, job_id=job_id, reason=REASON_UPLOAD_UNSUPPORTED)
            return
        if target not in TARGET_TO_JOB_TYPE:
            await self._reject(session, job_id=job_id, reason=REASON_INVALID_HEADER)
            return
        # Validate the peer-supplied filename — it becomes the
        # second path segment under
        # ``.esphome/.remote_builds/<dashboard_id>/<device_name>/``.
        # An unvalidated separator / ``..`` here would let a
        # malicious offloader write the assembled tarball
        # outside the intended subtree.
        device_stem = _validate_configuration_filename(frame["configuration_filename"])
        if device_stem is None:
            await self._reject(session, job_id=job_id, reason=REASON_INVALID_HEADER)
            return
        try:
            assembler = BundleAssembler(
                total_bytes=frame["total_bundle_bytes"],
                num_chunks=frame["num_chunks"],
                sha256_hex=frame["bundle_sha256"],
            )
        except BundleAssemblerError as exc:
            await self._reject(session, job_id=job_id, reason=exc.code.value)
            return

        self._inflight[session.dashboard_id] = _PendingSubmit(
            job_id=job_id,
            configuration_filename=frame["configuration_filename"],
            target=target,
            assembler=assembler,
            device_stem=device_stem,
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
            target_esphome_version=_coerce_version_field(frame.get("target_esphome_version")),
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
            await self._reject(
                session,
                job_id=safe_job_id(chunk_dict),
                reason=REASON_INVALID_CHUNK,
                drop_inflight=True,
                terminate_session=True,
            )
            return
        pending = self._inflight.get(session.dashboard_id)
        if pending is None:
            await self._reject(session, job_id=frame["job_id"], reason=REASON_NO_INFLIGHT)
            return
        if frame["job_id"] != pending.job_id:
            await self._reject(session, job_id=frame["job_id"], reason=REASON_JOB_ID_MISMATCH)
            return
        try:
            raw = decode_chunk(frame["data_b64"])
        except (binascii.Error, ValueError):
            await self._reject(
                session,
                job_id=pending.job_id,
                reason=REASON_CHUNK_DECODE_FAILED,
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
        """
        Pull the in-flight entry, finalise the bundle, ack, then extract + queue.

        The ack precedes the write + extract (which can span the offloader's
        ack timeout on a slow disk); a post-ack failure is reported as a
        terminal ``failed`` job-state frame. Drops the in-flight entry first
        so a later failure can't leave a closed assembler dangling.
        """
        self._inflight.pop(session.dashboard_id, None)
        if self._extracts.stopped:
            return
        try:
            assembled = pending.assembler.finalise()
        except BundleAssemblerError as exc:
            await self._reject_assembler(session, pending=pending, exc=exc)
            return
        # Ack acceptance now (bundle received + hash-validated). Echo the
        # offloader's ``job_id`` back so it can match the response; the
        # receiver-side id rides :attr:`FirmwareJob.remote_peer` in the fan-out.
        await self._send_ack_accepted(session, job_id=pending.job_id)
        # Off the receive loop so heartbeats and cancel_job stay serviced
        # while a slow extract runs.
        self._extracts.spawn(
            (session.dashboard_id, pending.job_id),
            _post_ack.run_post_ack_extract(
                self, session=session, pending=pending, bundle_bytes=assembled
            ),
        )

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
        cancel_key: tuple[str, str],
    ) -> FirmwareJob:
        """Write the tarball, extract it, queue and return the :class:`FirmwareJob`."""
        return await _post_ack.extract_and_queue(
            self,
            session=session,
            pending=pending,
            bundle_bytes=bundle_bytes,
            cancel_key=cancel_key,
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
        chunks, base64 garbage). Header-validation reasons and
        recoverable assembler codes leave the session intact so
        the offloader can retry on a fresh submit. (Post-ack
        extract / queue failures don't reach here — they surface
        as a ``failed`` job-state frame via
        :meth:`_report_post_ack_failure`.)

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
        from ..peer_link import TerminateReason  # noqa: PLC0415

        if drop_inflight:
            self._inflight.pop(session.dashboard_id, None)
        payload = SubmitJobAckFrameData(
            type="submit_job_ack", job_id=job_id, accepted=False, reason=reason
        )
        await session.send_app_frame(dict(payload))
        if terminate_session:
            await session.terminate(TerminateReason.MALFORMED_FRAME)
