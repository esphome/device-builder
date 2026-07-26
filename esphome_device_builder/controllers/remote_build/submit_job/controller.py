"""
Receiver-side ``submit_job`` flow for the remote-build peer-link.

Drives the post-handshake ``submit_job`` header +
``submit_job_chunk`` stream from the peer-link receive loop into
a queued :class:`FirmwareJob` carrying the offloader's
``dashboard_id`` in :attr:`FirmwareJob.remote_peer`. The
lifecycle fan-out the other direction lives in
:mod:`..job_fanout`.

Flow:

1. A ``submit_job`` header lands in
   :meth:`SubmitJobReceiver.handle_submit_job`, which validates
   it and registers a :class:`BundleAssembler` in ``_inflight``
   keyed on the session's ``dashboard_id``. One concurrent
   submit per session.
2. ``submit_job_chunk`` frames feed the assembler via
   :meth:`SubmitJobReceiver.handle_submit_job_chunk`. The
   ``is_last=True`` chunk finalises (byte count + sha256) and
   sends the accepted :class:`SubmitJobAckFrameData`; rejects
   carry a structured ``reason``, and wire-level misbehaviour
   also terminates with ``malformed_frame``.
3. The write + extract + enqueue runs off the receive loop in
   :mod:`._post_ack`, serialized per peer by
   :mod:`._extract_window`; post-ack failures surface as
   terminal ``job_state_changed`` frames.

On-disk layout (per-peer per-device subtree, sibling tarball) is
owned by :class:`helpers.remote_build_layout.RemoteBuildPath`.
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
    """Return *value* if it is a ``str`` within the display cap, else ``""``."""
    if not isinstance(value, str):
        return ""
    if len(value) > _DEVICE_DISPLAY_FIELD_MAX_LEN:
        return ""
    return value


def _coerce_version_field(value: Any) -> str:
    """
    Coerce the peer-supplied ``target_esphome_version`` to a PEP 440 string or ``""``.

    The value becomes a ``pip install`` argument; ``""`` means no provisioning.
    """
    return coerce_pep440_version(value, max_len=PAIRING_VERSION_MAX_LEN)


def _validate_configuration_filename(filename: str) -> str | None:
    r"""
    Return the device-name segment if *filename* is a safe leaf YAML, else ``None``.

    The segment becomes a path component of the extract subtree, so this is a
    security gate: rejects empty input, ``/`` ``\\`` or NUL characters, ``.`` /
    ``..`` stems, and any extension other than ``.yaml`` / ``.yml``
    (case-insensitive). ``"kitchen.yaml"`` → ``"kitchen"``.
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
    """
    Per-session in-flight bundle reception state.

    Constructed on a valid header, fed chunk-by-chunk, discarded when the
    submit completes or the session ends. One per session at a time.
    """

    job_id: str
    configuration_filename: str
    target: str
    assembler: BundleAssembler
    # Validated YAML stem off ``configuration_filename``; keys the
    # on-disk extract subtree.
    device_stem: str
    # Display strings off the SUBMIT_JOB header, stamped onto the
    # FirmwareJob for the firmware-tasks UI; empty for older
    # offloaders. Display-only, never path-bearing.
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
          empty bundle, etc.) — a ``submit_job_ack`` rejection,
          not a ``terminate``: the chunk stream hasn't started,
          the wire is still intact.
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
        """
        Feed *frame* into the in-flight assembler. On final chunk: ack + queue.

        Reject branches flow through :meth:`_reject`; happy-path completion
        through :meth:`_finalise_and_queue`.
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

        The ack precedes the off-loop write + extract; a post-ack failure is
        reported as a terminal ``failed`` job-state frame.
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
        """
        Reject helper for assembler errors.

        Codes in :data:`_RECOVERABLE_ASSEMBLER_ERRORS` ack-and-stay; anything
        else terminates the session with ``malformed_frame`` after the ack.
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
        """
        Single chokepoint for every reject path.

        Drops the in-flight entry when *drop_inflight* is true, sends
        ``submit_job_ack{accepted: False, reason}``, then terminates the
        session with ``malformed_frame`` when *terminate_session* is true.
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
